import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../core/haptics.dart';
import '../library/library_controller.dart';
import '../metadata/workbench_controller.dart';
import '../metadata/workbench_view.dart';
import '../player/player_controller.dart';
import '../sync/state_bundles.dart';
import '../sync/sync_manager.dart';
import '../ui/tokens.dart';
import 'assistant_settings.dart';
import 'audio_dsp.dart';
import 'auth_settings.dart';
import 'config_controller.dart';
import 'groups.dart';
import 'settings_search.dart';
import 'sync_hub.dart';
import '../ui/frost.dart';
import '../ui/page_header.dart';
import '../ui/glass_search_bar.dart';
import '../ui/toast.dart';

export 'settings_search.dart' show SettingsPage;

/// What the Settings pages act on.
class SettingsDeps {
  const SettingsDeps({
    required this.dataDir,
    required this.config,
    required this.library,
    required this.player,
    required this.haptics,
    this.sync,
    this.bundles,
    this.workbench,
  });

  final String dataDir;
  final ConfigController config;
  final LibraryController library;
  final ValueListenable<PlayerController?> player;
  final Haptics haptics;

  /// The Companion manager; null leaves Companion & Sync out (tests).
  final CompanionSyncManager? sync;

  /// State bundles (export, import, the backup upload); null leaves them
  /// out (tests).
  final StateBundles? bundles;

  /// The metadata workbench; null leaves Settings → Metadata out (a
  /// read-only library, tests).
  final WorkbenchController? workbench;
}

/// main.py's Settings tab as a pushed page (it sits outside the tab bar in
/// Flet too): settings.py's hub with its search, and the sub-pages the port
/// has. [initial] opens one directly (`switch_to_settings(subpage)`).
/// [highlight] opens the hub and smoothly scrolls to + pulses that entry.
Future<void> openSettings(
  BuildContext context,
  SettingsDeps deps, {
  SettingsPage? initial,
  SettingsPage? highlight,
}) => Navigator.of(context).push(
  MaterialPageRoute<void>(
    builder: (_) =>
        SettingsScreen(deps: deps, initial: initial, highlight: highlight),
  ),
);

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({
    super.key,
    required this.deps,
    this.initial,
    this.highlight,
  });

  final SettingsDeps deps;
  final SettingsPage? initial;
  final SettingsPage? highlight;

  @override
  State<SettingsScreen> createState() => SettingsScreenState();
}

@visibleForTesting
class SettingsScreenState extends State<SettingsScreen> {
  /// `_current_subpage_name`: null on the hub.
  late SettingsPage? page = widget.initial;
  final _search = TextEditingController();

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  void open(SettingsPage p) => setState(() => page = p);

  /// `_show_hub`, keeping the search.
  void showHub() => setState(() => page = null);

  @override
  Widget build(BuildContext context) {
    // main.py `navigate_back`: a sub-page goes back to the hub, the hub
    // leaves Settings.
    return PopScope(
      canPop: page == null,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        widget.deps.haptics.trigger('swipe_back');
        showHub();
      },
      child: Scaffold(
        body: SafeArea(
          top: false,
          child: AnimatedSwitcher(
            duration: const Duration(milliseconds: 180),
            child: page == null
                ? _Hub(
                    key: const ValueKey('hub'),
                    search: _search,
                    onOpen: open,
                    hasSync: widget.deps.sync != null,
                    hasWorkbench: widget.deps.workbench != null,
                    highlight: widget.highlight,
                  )
                : KeyedSubtree(key: ValueKey(page), child: _subpage(page!)),
          ),
        ),
      ),
    );
  }

  Widget _subpage(SettingsPage p) {
    final d = widget.deps;
    final body = switch (p) {
      SettingsPage.companion => switch (d.sync) {
        final m? => SyncHubGroup(
          manager: m,
          bundles: d.bundles,
          library: d.library,
        ),
        null => const Center(child: Dim('Companion & Sync is not running.')),
      },
      SettingsPage.storage => StorageGroup(deps: d),
      SettingsPage.account => AuthGroup(deps: d),
      SettingsPage.assistant => AssistantSettingsGroup(deps: d),
      SettingsPage.appearance => AppearanceGroup(deps: d),
      SettingsPage.audio => AudioDspGroup(deps: d),
      SettingsPage.haptics => HapticsGroup(deps: d),
      SettingsPage.metadata => switch (d.workbench) {
        final w? => _BelowHeader(child: WorkbenchPage(controller: w)),
        null => const Center(child: Dim('The library is read-only.')),
      },
      SettingsPage.permissions => const PermissionsGroup(),
      SettingsPage.database => DatabaseGroup(deps: d),
      SettingsPage.advanced => AdvancedGroup(deps: d),
      SettingsPage.about => const AboutGroup(),
    };
    return SubpageFrame(title: p.title, onBack: showHub, child: body);
  }
}

/// `_show_sub_page`: the back chevron and title over the group, in the
/// shared [PageHeader]. Scrolling groups pad by [HeaderInsets.topOf].
class SubpageFrame extends StatelessWidget {
  const SubpageFrame({
    super.key,
    required this.title,
    required this.onBack,
    required this.child,
  });

  final String title;
  final VoidCallback onBack;
  final Widget child;

  @override
  Widget build(BuildContext context) => PageHeader(
    title: title,
    leading: HeaderBackButton(onPressed: onBack),
    child: child,
  );
}

/// Pads a non-scrolling sub-page body below the floating header.
class _BelowHeader extends StatelessWidget {
  const _BelowHeader({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Padding(
    padding: EdgeInsets.only(top: HeaderInsets.topOf(context)),
    child: child,
  );
}

/// A sub-page's scrolling column, with settings.py's floating save button
/// (`_mark_dirty`) when [onSave] is set.
class GroupBody extends StatelessWidget {
  const GroupBody({
    super.key,
    required this.children,
    this.spacing = 16,
    this.onSave,
    this.saveLabel = 'SAVE CHANGES',
    this.saveIcon = Icons.save_rounded,
  });

  final List<Widget> children;
  final double spacing;
  final VoidCallback? onSave;
  final String saveLabel;
  final IconData saveIcon;

  @override
  Widget build(BuildContext context) {
    final hPad = Tokens.pagePad(context);
    return Stack(
      children: [
        ListView(
          padding: EdgeInsets.fromLTRB(
            hPad,
            HeaderInsets.topOf(context) + 4,
            hPad,
            100,
          ),
          children: [
            for (final (i, c) in children.indexed) ...[
              if (i > 0) SizedBox(height: spacing),
              c,
            ],
          ],
        ),
        if (onSave != null)
          Positioned(
            left: 0,
            right: 0,
            bottom: 15,
            child: Center(
              child: OnyxButton(
                label: saveLabel,
                icon: saveIcon,
                height: 46,
                onTap: onSave!,
              ),
            ),
          ),
      ],
    );
  }
}

/// ui/widgets.py `OnyxButton`: an accent pill with dark text.
class OnyxButton extends StatelessWidget {
  const OnyxButton({
    super.key,
    required this.label,
    required this.onTap,
    this.icon,
    this.height = 44,
    this.width,
    this.textSize = 13,
  });

  final String label;
  final IconData? icon;
  final VoidCallback? onTap;
  final double height;
  final double? width;
  final double textSize;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return SizedBox(
      height: height,
      width: width,
      child: FilledButton(
        onPressed: onTap,
        style: FilledButton.styleFrom(
          backgroundColor: accent,
          foregroundColor: Tokens.bg,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Tokens.radiusPill),
          ),
        ),
        child: Row(
          mainAxisSize: width == null ? MainAxisSize.min : MainAxisSize.max,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            if (icon != null) ...[
              Icon(icon, size: textSize < 13 ? 16 : 18),
              SizedBox(width: textSize < 13 ? 6 : 8),
            ],
            Text(
              label,
              style: TextStyle(fontWeight: FontWeight.w700, fontSize: textSize),
            ),
          ],
        ),
      ),
    );
  }
}

/// The bordered card settings.py builds its groups from.
class SettingsCard extends StatelessWidget {
  const SettingsCard({
    super.key,
    required this.child,
    this.padding = 16,
    this.radius = 12,
    this.borderColor = Tokens.border,
  });

  final Widget child;
  final double padding, radius;
  final Color borderColor;

  @override
  Widget build(BuildContext context) => GlassCard(
    padding: EdgeInsets.all(padding),
    radius: radius,
    flatBorder: borderColor,
    child: child,
  );
}

class Dim extends StatelessWidget {
  const Dim(this.text, {super.key, this.size = 12, this.color = Tokens.dim});

  final String text;
  final double size;
  final Color color;

  @override
  Widget build(BuildContext context) => Text(
    text,
    style: TextStyle(color: color, fontSize: size),
  );
}

/// A section label in the accent (settings.py's "Landing Page Sections").
class AccentLabel extends StatelessWidget {
  const AccentLabel(this.text, {super.key, this.size = 12});

  final String text;
  final double size;

  @override
  Widget build(BuildContext context) => Text(
    text,
    style: TextStyle(
      color: Theme.of(context).colorScheme.primary,
      fontSize: size,
      fontWeight: FontWeight.bold,
    ),
  );
}

void showSettingsSnack(BuildContext context, String text) =>
    showSnackOn(ScaffoldMessenger.of(context), text);

/// main.py `show_snackbar(text, icon, color)` on [m]: the icon in [color]
/// (the accent by default) before the text; a colour alone gets Flet's
/// default bell. Takes the messenger, so a job that outlives its page can
/// still report.
void showSnackOn(
  ScaffoldMessengerState m,
  String text, {
  IconData? icon,
  Color? color,
}) {
  icon ??= color == null ? null : Icons.notifications_rounded;
  final overlay = Overlay.maybeOf(m.context, rootOverlay: true);
  if (overlay != null) {
    showAppToastOn(overlay, text, icon: icon, color: color);
    return;
  }
  m
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: Text(text)));
}

// ---------------------------------------------------------------------------

/// `_render_hub_or_search`.
class _Hub extends StatefulWidget {
  const _Hub({
    super.key,
    required this.search,
    required this.onOpen,
    required this.hasSync,
    this.hasWorkbench = false,
    this.highlight,
  });

  final TextEditingController search;
  final ValueChanged<SettingsPage> onOpen;
  final bool hasSync;
  final bool hasWorkbench;
  final SettingsPage? highlight;

  @override
  State<_Hub> createState() => _HubState();
}

class _HubState extends State<_Hub> with SingleTickerProviderStateMixin {
  final _itemKeys = <SettingsPage, GlobalKey>{};
  AnimationController? _pulse;
  SettingsPage? _highlighted;

  GlobalKey _keyFor(SettingsPage p) =>
      _itemKeys.putIfAbsent(p, () => GlobalKey());

  @override
  void initState() {
    super.initState();
    widget.search.addListener(_changed);
    if (widget.highlight != null) {
      _highlighted = widget.highlight;
      _pulse = AnimationController(
        vsync: this,
        duration: const Duration(milliseconds: 1400),
      );
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        final ctx = _itemKeys[widget.highlight]?.currentContext;
        if (ctx != null) {
          Scrollable.ensureVisible(
            ctx,
            alignment: 0.45,
            duration: const Duration(milliseconds: 360),
            curve: Curves.easeOutCubic,
          );
        }
        _pulse?.forward(from: 0);
      });
    }
  }

  @override
  void dispose() {
    _pulse?.dispose();
    widget.search.removeListener(_changed);
    super.dispose();
  }

  void _changed() => setState(() {});

  SettingEntry _entry(SettingsPage p) =>
      kSettingEntries.firstWhere((e) => e.page == p);

  static Color _badgeColor(SettingsPage p, Color accent) => switch (p) {
    SettingsPage.appearance => Tokens.libArtist,
    SettingsPage.audio => Tokens.libAlbum,
    SettingsPage.haptics => Tokens.libGenre,
    SettingsPage.storage => Tokens.libPlaylist,
    SettingsPage.metadata => Tokens.libTrack,
    SettingsPage.companion => const Color(0xFF64D2FF),
    SettingsPage.account => Tokens.amber,
    SettingsPage.assistant => accent,
    SettingsPage.permissions => const Color(0xFF30D158),
    SettingsPage.database => const Color(0xFFFF453A),
    SettingsPage.advanced => const Color(0xFF8E8E93),
    SettingsPage.about => const Color(0xFF636366),
  };

  Widget _group(
    BuildContext context,
    String label,
    List<SettingEntry> entries, {
    bool showCategoryInSub = false,
  }) {
    final accent = Theme.of(context).colorScheme.primary;
    return Padding(
      padding: const EdgeInsets.only(bottom: 22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 4, bottom: 8),
            child: AccentLabel(label, size: 11),
          ),
          GlassCard(
            padding: EdgeInsets.zero,
            radius: Tokens.radiusCard + 2,
            child: Column(
              children: [
                for (final (i, e) in entries.indexed) ...[
                  if (i > 0)
                    const Divider(
                      height: 1,
                      indent: 58,
                      color: Tokens.borderSubtle,
                    ),
                  _HubItem(
                    key: showCategoryInSub ? null : _keyFor(e.page),
                    icon: e.icon,
                    badgeColor: _badgeColor(e.page, accent),
                    title: e.title,
                    subtitle: showCategoryInSub
                        ? '[${e.category}] ${e.subtitle}'
                        : e.subtitle,
                    highlightAnimation:
                        !showCategoryInSub && _highlighted == e.page
                        ? _pulse
                        : null,
                    onTap: () => widget.onOpen(e.page),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final query = widget.search.text.trim().toLowerCase();
    final hPad = Tokens.pagePad(context);
    final children = <Widget>[];
    if (query.isEmpty) {
      children.addAll([
        _group(context, 'LOOK & FEEL', [
          _entry(SettingsPage.appearance),
          _entry(SettingsPage.audio),
          _entry(SettingsPage.haptics),
        ]),
        _group(context, 'LIBRARY & SYNC', [
          _entry(SettingsPage.storage),
          if (widget.hasWorkbench) _entry(SettingsPage.metadata),
          if (widget.hasSync) _entry(SettingsPage.companion),
        ]),
        _group(context, 'SERVICES & AI', [
          _entry(SettingsPage.account),
          _entry(SettingsPage.assistant),
        ]),
        _group(context, 'SYSTEM', [
          _entry(SettingsPage.permissions),
          _entry(SettingsPage.database),
          _entry(SettingsPage.advanced),
          _entry(SettingsPage.about),
        ]),
      ]);
    } else {
      final matches = [
        for (final e in searchSettings(query))
          if ((widget.hasSync || e.page != SettingsPage.companion) &&
              (widget.hasWorkbench || e.page != SettingsPage.metadata))
            e,
      ];
      if (matches.isNotEmpty) {
        children.add(
          _group(
            context,
            'SEARCH RESULTS (${matches.length})',
            matches,
            showCategoryInSub: true,
          ),
        );
      } else {
        children.add(
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 30, horizontal: 16),
            child: Column(
              children: [
                const Icon(
                  Icons.search_off_rounded,
                  color: Tokens.dim,
                  size: 36,
                ),
                const SizedBox(height: 6),
                Dim("No settings found matching '$query'", size: 13),
                const SizedBox(height: 14),
                OnyxButton(
                  label: 'CLEAR SEARCH',
                  icon: Icons.clear_rounded,
                  textSize: 12,
                  height: 38,
                  onTap: widget.search.clear,
                ),
              ],
            ),
          ),
        );
      }
    }

    final navigator = Navigator.of(context);
    return PageHeader(
      title: 'Settings',
      leading: navigator.canPop()
          ? HeaderBackButton(onPressed: () => navigator.maybePop())
          : null,
      bottom: [
        Padding(
          padding: EdgeInsets.fromLTRB(hPad, 0, hPad, 8),
          child: GlassSearchBar(
            controller: widget.search,
            hintText: 'Search settings…',
            clearTooltip: 'Clear',
          ),
        ),
      ],
      child: Builder(
        builder: (context) => ListView(
          padding: EdgeInsets.fromLTRB(
            hPad,
            HeaderInsets.topOf(context) + 8,
            hPad,
            32,
          ),
          children: children,
        ),
      ),
    );
  }
}

/// Apple-style grouped hub row with a rounded-square tinted icon badge.
class _HubItem extends StatelessWidget {
  const _HubItem({
    super.key,
    required this.icon,
    required this.badgeColor,
    required this.title,
    required this.subtitle,
    required this.onTap,
    this.highlightAnimation,
  });

  final IconData icon;
  final Color badgeColor;
  final String title, subtitle;
  final VoidCallback onTap;
  final Animation<double>? highlightAnimation;

  @override
  Widget build(BuildContext context) {
    final row = Material(
      type: MaterialType.transparency,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              Container(
                width: 32,
                height: 32,
                decoration: BoxDecoration(
                  color: badgeColor.withValues(alpha: 0.18),
                  borderRadius: BorderRadius.circular(9),
                ),
                child: Icon(icon, color: badgeColor, size: 18),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontSize: 15,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Dim(subtitle, size: 12),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              Icon(
                Icons.chevron_right_rounded,
                color: Tokens.dim.withValues(alpha: 0.45),
                size: 18,
              ),
            ],
          ),
        ),
      ),
    );

    final anim = highlightAnimation;
    if (anim == null) return row;

    final accent = Theme.of(context).colorScheme.primary;
    return AnimatedBuilder(
      animation: anim,
      builder: (_, child) {
        final t = anim.value;
        // Quick ease-in pop (0..0.22), hold glow (0.22..0.55), smooth fade out (0.55..1.0).
        final glow = t < 0.22
            ? Curves.easeOut.transform(t / 0.22)
            : t < 0.55
            ? 1.0
            : 1.0 - Curves.easeIn.transform((t - 0.55) / 0.45);
        final pop = t < 0.20
            ? 1.0 + 0.025 * Curves.easeOutCubic.transform(t / 0.20)
            : t < 0.45
            ? 1.0 +
                  0.025 * (1.0 - Curves.easeInOut.transform((t - 0.20) / 0.25))
            : 1.0;
        return Transform.scale(
          scale: pop,
          child: DecoratedBox(
            decoration: BoxDecoration(
              color: accent.withValues(alpha: 0.18 * glow),
              border: Border.all(
                color: accent.withValues(alpha: 0.65 * glow),
                width: 1.25,
              ),
              borderRadius: BorderRadius.circular(Tokens.radiusCard),
            ),
            child: child,
          ),
        );
      },
      child: row,
    );
  }
}
