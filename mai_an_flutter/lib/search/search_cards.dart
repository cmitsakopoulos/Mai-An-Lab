import 'package:flutter/material.dart';

import '../downloads/download_dock.dart' show sourceColor;
import '../player/ui/artwork.dart';
import '../ui/tokens.dart';
import 'local_search.dart';
import 'search_controller.dart';

/// UI building blocks for `SearchScreen` (`StreamripApp/ui/views/search.py`):
/// source segmented pill, 4-bar connection signal, floating search/preview
/// status cards, skeleton rows, unified hero/album/artist cards, expandable
/// result rows, selection bar, pagination bar, and credentials/error banners.

Color searchRowAccent(
  String mediaType,
  Color fallback, {
  CategoryPalette? palette,
}) => switch (mediaType) {
  'artist' => palette?.artist ?? Tokens.libArtist,
  'album' => palette?.album ?? Tokens.libAlbum,
  'track' => palette?.track ?? Tokens.libTrack,
  _ => fallback,
};

IconData searchTypeIcon(String mediaType) => switch (mediaType) {
  'artist' => Icons.person_rounded,
  'album' => Icons.album_rounded,
  'track' => Icons.music_note_rounded,
  _ => Icons.music_note_rounded,
};

/// `search.py` `ConnectionSignal`: 4 bars with heights 4, 7, 10, 13.
class ConnectionSignal extends StatelessWidget {
  const ConnectionSignal({super.key, required this.state});

  final SignalState state;

  @override
  Widget build(BuildContext context) {
    if (!state.visible) return const SizedBox.shrink();
    final activeColor = state.connected
        ? const Color(0xFF00E676)
        : Theme.of(context).colorScheme.primary;
    final inactiveColor = activeColor.withValues(alpha: 0.2);
    const heights = [4.0, 7.0, 10.0, 13.0];
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        for (var i = 0; i < 4; i++) ...[
          if (i > 0) const SizedBox(width: 1.5),
          Container(
            width: 3,
            height: heights[i],
            decoration: BoxDecoration(
              color: i < state.level ? activeColor : inactiveColor,
              borderRadius: BorderRadius.circular(1),
            ),
          ),
        ],
      ],
    );
  }
}

/// Compact Qobuz / Deezer segmented source selector (`_source_bar`).
class SourceSegmentedBar extends StatelessWidget {
  const SourceSegmentedBar({
    super.key,
    required this.selected,
    required this.onChanged,
  });

  final String selected;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    const items = [('qobuz', 'Qobuz'), ('deezer', 'Deezer')];
    return Container(
      width: 184,
      height: 36,
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
        border: Border.all(color: Tokens.borderSubtle),
      ),
      child: Row(
        children: [
          for (final (key, label) in items)
            Expanded(
              child: GestureDetector(
                onTap: () => onChanged(key),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 160),
                  curve: Curves.easeOut,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: selected == key
                        ? sourceColor(key).withValues(alpha: 0.2)
                        : Colors.transparent,
                    borderRadius: BorderRadius.circular(Tokens.radiusPill),
                    border: selected == key
                        ? Border.all(
                            color: sourceColor(key).withValues(alpha: 0.5),
                          )
                        : null,
                  ),
                  child: Text(
                    label,
                    style: TextStyle(
                      color: selected == key ? sourceColor(key) : Tokens.dim,
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// Floating progress card overlaid at the top of the results area (used for
/// both search progress and stream preview progress).
class SearchOverlayCard extends StatelessWidget {
  const SearchOverlayCard({
    super.key,
    required this.status,
    required this.detail,
    this.onCancel,
  });

  final String status;
  final String detail;
  final VoidCallback? onCancel;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: Tokens.surface,
        border: Border.all(color: accent.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(12),
        boxShadow: const [
          BoxShadow(blurRadius: 16, spreadRadius: -4, color: Color(0x66000000)),
        ],
      ),
      child: Row(
        children: [
          SizedBox(
            width: 18,
            height: 18,
            child: CircularProgressIndicator(strokeWidth: 2, color: accent),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  status,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                if (detail.isNotEmpty) ...[
                  const SizedBox(height: 2),
                  Text(
                    detail,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Tokens.dim, fontSize: 11),
                  ),
                ],
              ],
            ),
          ),
          if (onCancel != null)
            IconButton(
              icon: const Icon(Icons.close_rounded, size: 16),
              color: Tokens.dim,
              tooltip: 'Cancel Preview',
              constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
              padding: EdgeInsets.zero,
              onPressed: onCancel,
            ),
        ],
      ),
    );
  }
}

/// Compact skeleton placeholder row shown while remote search runs.
class SearchSkeletonRow extends StatelessWidget {
  const SearchSkeletonRow({super.key});

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 58,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Tokens.surface.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
        border: Border.all(color: Tokens.borderSubtle),
      ),
      child: Row(
        children: [
          Container(
            width: 24,
            height: 24,
            decoration: BoxDecoration(
              color: Tokens.surface2,
              borderRadius: BorderRadius.circular(6),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Container(
                  height: 11,
                  width: 160,
                  decoration: BoxDecoration(
                    color: Tokens.surface2,
                    borderRadius: BorderRadius.circular(4),
                  ),
                ),
                const SizedBox(height: 6),
                Container(
                  height: 9,
                  width: 100,
                  decoration: BoxDecoration(
                    color: Tokens.surface2.withValues(alpha: 0.7),
                    borderRadius: BorderRadius.circular(4),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Banner shown at the top of the Search landing when neither Qobuz nor Deezer
/// credentials are configured yet.
class MissingCredentialsBanner extends StatelessWidget {
  const MissingCredentialsBanner({super.key, required this.onConnect});

  final VoidCallback onConnect;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      margin: const EdgeInsets.only(bottom: 16),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Tokens.surface,
        border: Border.all(color: accent.withValues(alpha: 0.3)),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
      ),
      child: Row(
        children: [
          Container(
            width: 40,
            height: 40,
            decoration: BoxDecoration(
              color: accent.withValues(alpha: 0.14),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Icon(Icons.lock_person_rounded, color: accent, size: 20),
          ),
          const SizedBox(width: 12),
          const Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Streaming Setup',
                  style: TextStyle(
                    color: Tokens.text,
                    fontSize: 14,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                SizedBox(height: 2),
                Text(
                  'Link Qobuz or Deezer in Settings for lossless search & downloads.',
                  style: TextStyle(color: Tokens.dim, fontSize: 11),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: accent,
              foregroundColor: Tokens.bg,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              minimumSize: const Size(0, 34),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(10),
              ),
            ),
            onPressed: onConnect,
            child: const Text(
              'Connect',
              style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700),
            ),
          ),
        ],
      ),
    );
  }
}

/// Inline remote-error banner shown above local library matches.
class SearchErrorBanner extends StatelessWidget {
  const SearchErrorBanner({
    super.key,
    required this.sourceLabel,
    required this.message,
    required this.onSettings,
    required this.onRetry,
  });

  final String sourceLabel;
  final String message;
  final VoidCallback? onSettings;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    const errColor = Color(0xFFFF6B6B);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(12, 8, 6, 8),
      decoration: BoxDecoration(
        color: errColor.withValues(alpha: 0.08),
        border: Border.all(color: errColor.withValues(alpha: 0.3)),
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
      ),
      child: Row(
        children: [
          const Icon(Icons.cloud_off_rounded, color: errColor, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  "Couldn't search $sourceLabel",
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 1),
                Text(
                  message,
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Tokens.dim, fontSize: 11),
                ),
              ],
            ),
          ),
          if (onSettings != null)
            TextButton(onPressed: onSettings, child: const Text('Settings')),
          TextButton(onPressed: onRetry, child: const Text('Retry')),
        ],
      ),
    );
  }
}

/// Full-area error view shown when remote search fails and no local hits exist.
class SearchErrorState extends StatelessWidget {
  const SearchErrorState({
    super.key,
    required this.message,
    required this.onSettings,
    required this.onRetry,
  });

  final String message;
  final VoidCallback? onSettings;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(
              Icons.cloud_off_rounded,
              color: Color(0xFFFF6B6B),
              size: 48,
            ),
            const SizedBox(height: 8),
            const Text(
              "Search couldn't connect",
              style: TextStyle(
                color: Tokens.text,
                fontSize: 18,
                fontWeight: FontWeight.bold,
              ),
            ),
            const SizedBox(height: 6),
            SelectableText(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(color: Tokens.dim, fontSize: 12),
            ),
            const SizedBox(height: 16),
            Wrap(
              spacing: 10,
              runSpacing: 8,
              alignment: WrapAlignment.center,
              children: [
                if (onSettings != null)
                  FilledButton.icon(
                    style: FilledButton.styleFrom(
                      backgroundColor: accent,
                      foregroundColor: Tokens.bg,
                    ),
                    onPressed: onSettings,
                    icon: const Icon(Icons.settings_rounded, size: 16),
                    label: const Text('Check Settings'),
                  ),
                TextButton.icon(
                  onPressed: onRetry,
                  icon: const Icon(Icons.refresh_rounded, size: 16),
                  label: const Text('Retry'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

/// Selection action bar swapping into the mode-bar slot during multi-select.
class SearchSelectionBar extends StatelessWidget {
  const SearchSelectionBar({
    super.key,
    required this.count,
    required this.onExit,
    required this.onSelectAll,
    required this.onDownload,
  });

  final int count;
  final VoidCallback onExit;
  final VoidCallback onSelectAll;
  final VoidCallback onDownload;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      height: 40,
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(4, 2, 6, 2),
      decoration: BoxDecoration(
        color: Tokens.surface,
        border: Border.all(color: accent.withValues(alpha: 0.35)),
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
      ),
      child: Row(
        children: [
          IconButton(
            icon: const Icon(Icons.close_rounded, size: 18),
            color: Tokens.dim,
            tooltip: 'Exit selection',
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
            style: IconButton.styleFrom(
              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
            ),
            onPressed: onExit,
          ),
          const SizedBox(width: 2),
          Expanded(
            child: Text(
              count > 0 ? '$count selected' : 'Select items',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                color: Tokens.text,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          TextButton(
            style: TextButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
              minimumSize: Size.zero,
              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
            ),
            onPressed: onSelectAll,
            child: Text(
              'Select all',
              style: TextStyle(
                color: accent,
                fontSize: 12,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          if (count > 0) ...[
            const SizedBox(width: 4),
            InkWell(
              borderRadius: BorderRadius.circular(Tokens.radiusPill),
              onTap: onDownload,
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 11,
                  vertical: 6,
                ),
                decoration: BoxDecoration(
                  color: accent,
                  borderRadius: BorderRadius.circular(Tokens.radiusPill),
                ),
                child: const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.download_rounded, color: Tokens.bg, size: 15),
                    SizedBox(width: 4),
                    Text(
                      'Download',
                      style: TextStyle(
                        color: Tokens.bg,
                        fontSize: 12,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

/// Ownership helper reading the stamped fields on a remote result map.
OwnershipInfo ownershipFromResult(Map<String, Object?> r) => OwnershipInfo(
  mediaType: '${r['media_type'] ?? 'track'}',
  isInLibrary: r['is_in_library'] == true,
  ownedTracks: (r['owned_tracks'] as num?)?.toInt() ?? 0,
  numTracks: (r['num_tracks'] as num?)?.toInt() ?? 0,
);

/// `search.py` `_build_hero_result_card`: gradient top-result spotlight card.
class HeroResultCard extends StatelessWidget {
  const HeroResultCard({
    super.key,
    required this.hero,
    required this.onTapCard,
    required this.onDownload,
  });

  final Map<String, Object?> hero;
  final VoidCallback onTapCard;
  final VoidCallback onDownload;

  @override
  Widget build(BuildContext context) {
    final primary = Theme.of(context).colorScheme.primary;
    final mType = '${hero['media_type'] ?? 'track'}';
    final accent = searchRowAccent(
      mType,
      primary,
      palette: CategoryPalette.of(context),
    );
    final title = stripMarkup(
      '${hero['ui_title'] ?? hero['name'] ?? hero['title'] ?? 'Unknown'}',
    );
    final subtitle = stripMarkup(
      '${hero['ui_subtitle'] ?? hero['artist'] ?? ''}',
    );
    final detail = stripMarkup('${hero['ui_detail'] ?? ''}');
    final cover = '${hero['cover'] ?? hero['image'] ?? ''}';
    final ownership = ownershipFromResult(hero);
    final isInLibrary = ownership.isInLibrary;
    final subText =
        subtitle.isNotEmpty &&
            subtitle.trim().toLowerCase() != title.trim().toLowerCase()
        ? subtitle
        : '';

    final double radius = mType == 'artist' ? 44 : 12;
    final artwork = Container(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(radius),
        border: Border.all(
          color: accent.withValues(alpha: mType == 'artist' ? 0.35 : 0.2),
          width: mType == 'artist' ? 2 : 1,
        ),
      ),
      child: Artwork(
        path: cover.isEmpty ? null : cover,
        size: 88,
        radius: radius,
        placeholder: searchTypeIcon(mType),
        placeholderSize: 40,
        cacheWidth: 176,
      ),
    );

    final Widget action;
    if (mType == 'artist') {
      action = InkWell(
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
        onTap: onTapCard,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
          decoration: BoxDecoration(
            color: primary.withValues(alpha: 0.12),
            border: Border.all(color: primary.withValues(alpha: 0.3)),
            borderRadius: BorderRadius.circular(Tokens.radiusPill),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'All artists',
                style: TextStyle(
                  color: primary,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 4),
              Icon(Icons.arrow_forward_rounded, size: 15, color: primary),
            ],
          ),
        ),
      );
    } else if (mType == 'album') {
      final col = isInLibrary ? primary : Tokens.text;
      action = InkWell(
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
        onTap: onDownload,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
          decoration: BoxDecoration(
            color: col.withValues(alpha: 0.15),
            border: Border.all(color: col.withValues(alpha: 0.35)),
            borderRadius: BorderRadius.circular(Tokens.radiusPill),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                isInLibrary
                    ? Icons.check_circle_rounded
                    : Icons.arrow_circle_down_rounded,
                size: 16,
                color: col,
              ),
              const SizedBox(width: 5),
              Text(
                isInLibrary ? 'In Library' : 'Download',
                style: TextStyle(
                  color: col,
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      );
    } else {
      action = IconButton(
        icon: Icon(
          isInLibrary
              ? Icons.check_circle_rounded
              : Icons.arrow_circle_down_rounded,
          size: 24,
        ),
        color: isInLibrary ? primary : Tokens.text,
        tooltip: isInLibrary ? 'Redownload' : 'Download',
        onPressed: onDownload,
      );
    }

    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: Colors.transparent,
        borderRadius: BorderRadius.circular(Tokens.radiusCard + 4),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTapCard,
          child: Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [accent.withValues(alpha: 0.18), Tokens.surface],
              ),
              border: Border.all(color: accent.withValues(alpha: 0.28)),
              borderRadius: BorderRadius.circular(Tokens.radiusCard + 4),
            ),
            child: Row(
              children: [
                artwork,
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 6,
                        runSpacing: 4,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          Container(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 8,
                              vertical: 2,
                            ),
                            decoration: BoxDecoration(
                              color: accent.withValues(alpha: 0.16),
                              border: Border.all(
                                color: accent.withValues(alpha: 0.3),
                              ),
                              borderRadius: BorderRadius.circular(
                                Tokens.radiusPill,
                              ),
                            ),
                            child: Text(
                              'TOP RESULT · ${mType.toUpperCase()}',
                              style: TextStyle(
                                color: accent,
                                fontSize: 9,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                          OwnershipBadge(ownership: ownership),
                        ],
                      ),
                      const SizedBox(height: 4),
                      Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 18,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      if (subText.isNotEmpty) ...[
                        const SizedBox(height: 2),
                        Text(
                          subText,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Tokens.dim,
                            fontSize: 12,
                          ),
                        ),
                      ],
                      if (detail.isNotEmpty) ...[
                        const SizedBox(height: 2),
                        Text(
                          detail,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Tokens.textTertiary,
                            fontSize: 11,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(width: 8),
                action,
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Section header for Unified `"All"` view (`Top Songs`, `Albums & EPs`,
/// `Artists`), with optional horizontal shelf navigation chevrons.
class SearchSectionHeader extends StatelessWidget {
  const SearchSectionHeader({
    super.key,
    required this.title,
    required this.count,
    required this.icon,
    required this.onSeeAll,
    this.onScrollLeft,
    this.onScrollRight,
  });

  final String title;
  final int count;
  final IconData icon;
  final VoidCallback onSeeAll;
  final VoidCallback? onScrollLeft;
  final VoidCallback? onScrollRight;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Padding(
      padding: const EdgeInsets.fromLTRB(2, 14, 2, 6),
      child: Row(
        children: [
          Icon(icon, color: accent, size: 16),
          const SizedBox(width: 6),
          Text(
            title,
            style: const TextStyle(
              color: Tokens.text,
              fontSize: 14,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(width: 6),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
            decoration: BoxDecoration(
              color: Tokens.surface2,
              borderRadius: BorderRadius.circular(Tokens.radiusPill),
            ),
            child: Text(
              '$count',
              style: const TextStyle(
                color: Tokens.dim,
                fontSize: 10,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          const Spacer(),
          if (onScrollLeft != null && onScrollRight != null) ...[
            IconButton(
              icon: const Icon(Icons.chevron_left_rounded, size: 18),
              color: Tokens.dim,
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
              tooltip: 'Scroll left',
              onPressed: onScrollLeft,
            ),
            IconButton(
              icon: const Icon(Icons.chevron_right_rounded, size: 18),
              color: Tokens.dim,
              padding: EdgeInsets.zero,
              constraints: const BoxConstraints(minWidth: 28, minHeight: 28),
              tooltip: 'Scroll right',
              onPressed: onScrollRight,
            ),
          ],
          InkWell(
            borderRadius: BorderRadius.circular(8),
            onTap: onSeeAll,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    'See all ($count)',
                    style: TextStyle(
                      color: accent,
                      fontSize: 11,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  Icon(Icons.chevron_right_rounded, size: 15, color: accent),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Horizontal album cards shelf for Unified `"All"` mode.
class UnifiedAlbumShelf extends StatefulWidget {
  const UnifiedAlbumShelf({
    super.key,
    required this.albums,
    required this.totalCount,
    required this.onSeeAll,
    required this.onDownload,
  });

  final List<Map<String, Object?>> albums;
  final int totalCount;
  final VoidCallback onSeeAll;
  final ValueChanged<Map<String, Object?>> onDownload;

  @override
  State<UnifiedAlbumShelf> createState() => _UnifiedAlbumShelfState();
}

class _UnifiedAlbumShelfState extends State<UnifiedAlbumShelf> {
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
    final accent = Theme.of(context).colorScheme.primary;
    final shown = widget.albums.take(12).toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SearchSectionHeader(
          title: 'Albums & EPs',
          count: widget.totalCount,
          icon: Icons.album_rounded,
          onSeeAll: widget.onSeeAll,
          onScrollLeft: shown.length > 3 ? () => _nudge(-260) : null,
          onScrollRight: shown.length > 3 ? () => _nudge(260) : null,
        ),
        SizedBox(
          height: 235,
          child: ListView.separated(
            controller: _scroll,
            scrollDirection: Axis.horizontal,
            itemCount: shown.length,
            separatorBuilder: (_, _) => const SizedBox(width: 12),
            itemBuilder: (_, i) {
              final alb = shown[i];
              final title = stripMarkup(
                '${alb['ui_title'] ?? alb['title'] ?? alb['name'] ?? 'Unknown'}',
              );
              final artist = stripMarkup(
                '${alb['ui_subtitle'] ?? alb['artist'] ?? ''}',
              );
              final detail = stripMarkup('${alb['ui_detail'] ?? ''}');
              final cover = '${alb['cover'] ?? alb['image'] ?? ''}';
              final owned = alb['is_in_library'] == true;
              return InkWell(
                borderRadius: BorderRadius.circular(Tokens.radiusCard),
                onTap: widget.onSeeAll,
                child: Container(
                  width: 146,
                  padding: const EdgeInsets.all(7),
                  decoration: BoxDecoration(
                    color: Tokens.surface,
                    border: Border.all(color: Tokens.borderSubtle),
                    borderRadius: BorderRadius.circular(Tokens.radiusCard),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Stack(
                        children: [
                          Artwork(
                            path: cover.isEmpty ? null : cover,
                            size: 130,
                            radius: 10,
                            placeholder: Icons.album_rounded,
                            placeholderSize: 38,
                            cacheWidth: 260,
                          ),
                          if (owned)
                            Positioned(
                              top: 6,
                              right: 6,
                              child: Container(
                                padding: const EdgeInsets.all(3),
                                decoration: BoxDecoration(
                                  color: Tokens.surface.withValues(alpha: 0.85),
                                  borderRadius: BorderRadius.circular(
                                    Tokens.radiusPill,
                                  ),
                                ),
                                child: Icon(
                                  Icons.check_circle_rounded,
                                  color: accent,
                                  size: 16,
                                ),
                              ),
                            ),
                        ],
                      ),
                      const SizedBox(height: 5),
                      Text(
                        title,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      Text(
                        artist,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Tokens.dim, fontSize: 11),
                      ),
                      const Spacer(),
                      Row(
                        children: [
                          Expanded(
                            child: Text(
                              detail,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: Tokens.textTertiary,
                                fontSize: 10,
                              ),
                            ),
                          ),
                          IconButton(
                            icon: Icon(
                              owned
                                  ? Icons.check_circle_rounded
                                  : Icons.arrow_circle_down_rounded,
                              size: 18,
                            ),
                            color: owned ? accent : Tokens.dim,
                            padding: EdgeInsets.zero,
                            constraints: const BoxConstraints(
                              minWidth: 24,
                              minHeight: 24,
                            ),
                            style: IconButton.styleFrom(
                              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                            ),
                            tooltip: owned
                                ? 'In library — download again'
                                : 'Download album',
                            onPressed: () => widget.onDownload(alb),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

/// Horizontal circular artist cards shelf for Unified `"All"` mode.
class UnifiedArtistShelf extends StatefulWidget {
  const UnifiedArtistShelf({
    super.key,
    required this.artists,
    required this.totalCount,
    required this.onSeeAll,
  });

  final List<Map<String, Object?>> artists;
  final int totalCount;
  final VoidCallback onSeeAll;

  @override
  State<UnifiedArtistShelf> createState() => _UnifiedArtistShelfState();
}

class _UnifiedArtistShelfState extends State<UnifiedArtistShelf> {
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
    final accent = Theme.of(context).colorScheme.primary;
    final shown = widget.artists.take(8).toList();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SearchSectionHeader(
          title: 'Artists',
          count: widget.totalCount,
          icon: Icons.person_rounded,
          onSeeAll: widget.onSeeAll,
          onScrollLeft: shown.length > 3 ? () => _nudge(-260) : null,
          onScrollRight: shown.length > 3 ? () => _nudge(260) : null,
        ),
        SizedBox(
          height: 136,
          child: ListView.separated(
            controller: _scroll,
            scrollDirection: Axis.horizontal,
            itemCount: shown.length,
            separatorBuilder: (_, _) => const SizedBox(width: 14),
            itemBuilder: (_, i) {
              final art = shown[i];
              final name = stripMarkup(
                '${art['ui_title'] ?? art['name'] ?? 'Unknown'}',
              );
              final sub = stripMarkup('${art['ui_subtitle'] ?? 'Artist'}');
              final cover = '${art['cover'] ?? art['image'] ?? ''}';
              return InkWell(
                borderRadius: BorderRadius.circular(12),
                onTap: widget.onSeeAll,
                child: SizedBox(
                  width: 96,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Container(
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          border: Border.all(
                            color: accent.withValues(alpha: 0.25),
                            width: 2,
                          ),
                        ),
                        child: Artwork(
                          path: cover.isEmpty ? null : cover,
                          size: 74,
                          radius: 37,
                          placeholder: Icons.person_rounded,
                          placeholderSize: 32,
                          cacheWidth: 148,
                        ),
                      ),
                      const SizedBox(height: 5),
                      Text(
                        name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        textAlign: TextAlign.center,
                        style: const TextStyle(
                          color: Tokens.text,
                          fontSize: 11,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      Text(
                        sub,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        textAlign: TextAlign.center,
                        style: const TextStyle(color: Tokens.dim, fontSize: 10),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

/// Single remote search result row (track, album, artist, load_more_artist,
/// or search_exhausted).
class SearchResultRow extends StatelessWidget {
  const SearchResultRow({
    super.key,
    required this.item,
    required this.depth,
    required this.selectionMode,
    required this.selected,
    required this.expanded,
    required this.loadingChildren,
    required this.loadingMore,
    required this.playing,
    required this.previewState,
    required this.onTap,
    required this.onLongPress,
    required this.onDownload,
    required this.onPreview,
    required this.onLoadMore,
  });

  final Map<String, Object?> item;
  final int depth;
  final bool selectionMode;
  final bool selected;
  final bool expanded;
  final bool loadingChildren;
  final bool loadingMore;
  final bool playing;
  final String previewState;
  final VoidCallback onTap;
  final VoidCallback onLongPress;
  final VoidCallback onDownload;
  final VoidCallback onPreview;
  final VoidCallback onLoadMore;

  @override
  Widget build(BuildContext context) {
    final primary = Theme.of(context).colorScheme.primary;
    final mType = '${item['media_type'] ?? 'track'}';

    if (mType == 'load_more_artist') {
      return Padding(
        padding: EdgeInsets.fromLTRB(depth * 20.0 + 16, 6, 16, 6),
        child: InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: loadingMore ? null : onLoadMore,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
            decoration: BoxDecoration(
              color: primary.withValues(alpha: 0.1),
              border: Border.all(color: primary.withValues(alpha: 0.3)),
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                if (loadingMore)
                  SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: primary,
                    ),
                  )
                else
                  Icon(Icons.add_circle_outline, color: primary, size: 20),
                const SizedBox(width: 10),
                Text(
                  loadingMore ? 'Loading...' : 'Load More Albums',
                  style: TextStyle(
                    color: primary,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    }

    if (mType == 'search_exhausted') {
      return Padding(
        padding: EdgeInsets.only(left: depth * 20.0, top: 14, bottom: 14),
        child: const Center(
          child: Text(
            '; End of Discography ;',
            style: TextStyle(
              color: Tokens.dim,
              fontSize: 11,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
      );
    }

    final accent = searchRowAccent(
      mType,
      primary,
      palette: CategoryPalette.of(context),
    );
    final selectable = SearchTabController.isSelectable(item);
    final highlighted = playing || previewState != 'idle' || expanded;
    final bg = selected
        ? primary.withValues(alpha: 0.20)
        : highlighted
        ? accent.withValues(alpha: 0.12)
        : Colors.transparent;

    final title = stripMarkup(
      '${item['ui_title'] ?? item['name'] ?? 'Unknown'}',
    );
    final subtitle = stripMarkup(
      '${item['ui_subtitle'] ?? item['artist'] ?? ''}',
    );
    final detail = stripMarkup('${item['ui_detail'] ?? ''}');
    final showArtist =
        subtitle.isNotEmpty &&
        subtitle.trim().toLowerCase() != title.trim().toLowerCase();
    final subLine = [
      if (showArtist) subtitle,
      if (detail.isNotEmpty) detail,
    ].join('  ·  ');

    final source = '${item['source'] ?? 'qobuz'}'.toLowerCase();
    final srcTint = sourceColor(source);
    final ownership = ownershipFromResult(item);
    final isInLibrary = ownership.isInLibrary;

    final Widget leadingIcon;
    if (!selectionMode) {
      leadingIcon = Icon(searchTypeIcon(mType), color: accent, size: 18);
    } else if (!selectable) {
      leadingIcon = Icon(
        searchTypeIcon(mType),
        color: accent.withValues(alpha: 0.3),
        size: 18,
      );
    } else {
      leadingIcon = Icon(
        selected
            ? Icons.check_circle_rounded
            : Icons.radio_button_unchecked_rounded,
        color: selected ? primary : Tokens.dim,
        size: 20,
      );
    }

    return Material(
      color: bg,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        borderRadius: BorderRadius.circular(10),
        onTap: onTap,
        onLongPress: onLongPress,
        child: Padding(
          padding: EdgeInsets.fromLTRB(10 + depth * 16.0, 8, 6, 8),
          child: Row(
            children: [
              leadingIcon,
              const SizedBox(width: 12),
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
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 3),
                    Row(
                      children: [
                        Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 4,
                            vertical: 1,
                          ),
                          decoration: BoxDecoration(
                            color: srcTint.withValues(alpha: 0.15),
                            borderRadius: BorderRadius.circular(3),
                          ),
                          child: Text(
                            source.toUpperCase(),
                            style: TextStyle(
                              color: srcTint,
                              fontSize: 8,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        if (ownership.badgeLabel != null) ...[
                          const SizedBox(width: 6),
                          OwnershipBadge(ownership: ownership),
                        ],
                        if (subLine.isNotEmpty) ...[
                          const SizedBox(width: 6),
                          Expanded(
                            child: Text(
                              subLine,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: const TextStyle(
                                color: Tokens.dim,
                                fontSize: 12,
                              ),
                            ),
                          ),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
              if (!selectionMode) ...[
                if (mType == 'track')
                  IconButton(
                    icon: previewState == 'loading'
                        ? SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(
                              strokeWidth: 2,
                              color: primary,
                            ),
                          )
                        : Icon(
                            previewState == 'playing'
                                ? Icons.pause_circle_filled_rounded
                                : Icons.play_circle_filled_rounded,
                            size: 22,
                            color: previewState != 'idle'
                                ? primary
                                : Tokens.dim,
                          ),
                    tooltip: 'Preview',
                    constraints: const BoxConstraints(
                      minWidth: 36,
                      minHeight: 36,
                    ),
                    padding: EdgeInsets.zero,
                    onPressed: onPreview,
                  ),
                if (mType == 'track' || mType == 'album')
                  IconButton(
                    icon: Icon(
                      isInLibrary
                          ? Icons.check_circle_rounded
                          : Icons.arrow_circle_down_rounded,
                      size: 20,
                    ),
                    color: isInLibrary ? primary : Tokens.dim,
                    tooltip: isInLibrary ? 'Redownload' : 'Download',
                    constraints: const BoxConstraints(
                      minWidth: 36,
                      minHeight: 36,
                    ),
                    padding: EdgeInsets.zero,
                    onPressed: onDownload,
                  ),
                if (mType == 'artist' || mType == 'album')
                  SizedBox(
                    width: 24,
                    height: 24,
                    child: Center(
                      child: loadingChildren
                          ? SizedBox(
                              width: 15,
                              height: 15,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: accent,
                              ),
                            )
                          : Icon(
                              expanded
                                  ? Icons.keyboard_arrow_down_rounded
                                  : Icons.chevron_right_rounded,
                              color: expanded ? accent : Tokens.dim,
                              size: 18,
                            ),
                    ),
                  ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

/// `search.py` `_pagination_bar` with tap arrows and horizontal swipe gesture.
class SearchPaginationBar extends StatelessWidget {
  const SearchPaginationBar({
    super.key,
    required this.currentPage,
    required this.totalPages,
    required this.onChangePage,
  });

  final int currentPage;
  final int totalPages;
  final ValueChanged<int> onChangePage;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final hasPrev = currentPage > 0;
    final hasNext = currentPage < totalPages - 1;
    return GestureDetector(
      onHorizontalDragEnd: (details) {
        final vx = details.primaryVelocity ?? 0;
        if (vx.abs() < 300) return;
        if (vx < 0 && hasNext) onChangePage(currentPage + 1);
        if (vx > 0 && hasPrev) onChangePage(currentPage - 1);
      },
      child: Container(
        margin: const EdgeInsets.fromLTRB(14, 4, 14, 6),
        padding: const EdgeInsets.symmetric(vertical: 2, horizontal: 16),
        decoration: BoxDecoration(
          color: Tokens.surface.withValues(alpha: 0.7),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: accent.withValues(alpha: 0.15)),
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            IconButton(
              icon: const Icon(Icons.chevron_left_rounded, size: 20),
              color: hasPrev ? accent : Tokens.dim,
              tooltip: 'Previous Page',
              onPressed: hasPrev ? () => onChangePage(currentPage - 1) : null,
            ),
            const SizedBox(width: 12),
            Text(
              'Page ${currentPage + 1} of $totalPages',
              style: const TextStyle(
                color: Tokens.text,
                fontSize: 12,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(width: 12),
            IconButton(
              icon: const Icon(Icons.chevron_right_rounded, size: 20),
              color: hasNext ? accent : Tokens.dim,
              tooltip: 'Next Page',
              onPressed: hasNext ? () => onChangePage(currentPage + 1) : null,
            ),
          ],
        ),
      ),
    );
  }
}
