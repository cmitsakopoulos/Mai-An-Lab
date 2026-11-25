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
Future<void> openSettings(
  BuildContext context,
  SettingsDeps deps, {
  SettingsPage? initial,
}) => Navigator.of(context).push(
  MaterialPageRoute<void>(
    builder: (_) => SettingsScreen(deps: deps, initial: initial),
  ),
);

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key, required this.deps, this.initial});

  final SettingsDeps deps;
  final SettingsPage? initial;

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
          child: AnimatedSwitcher(
            duration: const Duration(milliseconds: 180),
            child: page == null
                ? _Hub(
                    key: const ValueKey('hub'),
                    search: _search,
                    onOpen: open,
                    hasSync: widget.deps.sync != null,
                    hasWorkbench: widget.deps.workbench != null,
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
        final w? => WorkbenchPage(controller: w),
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

/// `_show_sub_page`: the back chevron and title over the group.
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
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final hPad = Tokens.pagePad(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: EdgeInsets.fromLTRB(hPad - 6, 8, hPad, 8),
          child: Row(
            children: [
              IconButton(
                icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 17),
                color: accent,
                tooltip: 'Back',
                onPressed: onBack,
              ),
              const SizedBox(width: 4),
              Expanded(
                child: Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 20,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
        Expanded(child: child),
      ],
    );
  }
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
          padding: EdgeInsets.fromLTRB(hPad, 4, hPad, 100),
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

/// An icon and a bold title, the head of most cards.
class CardTitle extends StatelessWidget {
  const CardTitle(this.icon, this.title, {super.key, this.size = 14});

  final IconData icon;
  final String title;
  final double size;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      Icon(icon, color: Tokens.dim, size: 20),
      const SizedBox(width: 12),
      Expanded(
        child: Text(
          title,
          style: TextStyle(
            color: Tokens.text,
            fontSize: size,
            fontWeight: FontWeight.bold,
          ),
        ),
      ),
    ],
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
  });

  final TextEditingController search;
  final ValueChanged<SettingsPage> onOpen;
  final bool hasSync;
  final bool hasWorkbench;

  @override
  State<_Hub> createState() => _HubState();
}

class _HubState extends State<_Hub> {
  @override
  void initState() {
    super.initState();
    widget.search.addListener(_changed);
  }

  @override
  void dispose() {
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
                    icon: e.icon,
                    badgeColor: _badgeColor(e.page, accent),
                    title: e.title,
                    subtitle: showCategoryInSub
                        ? '[${e.category}] ${e.subtitle}'
                        : e.subtitle,
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

    const topChromeHeight = 148.0;
    return Stack(
      fit: StackFit.expand,
      children: [
        ListView(
          padding: EdgeInsets.fromLTRB(hPad, topChromeHeight, hPad, 32),
          children: children,
        ),
        Positioned(
          top: 0,
          left: 0,
          right: 0,
          child: DecoratedBox(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                stops: [0.0, 0.65, 1.0],
                colors: [
                  Tokens.bg,
                  Color(0xCC000000),
                  Color(0x00000000),
                ],
              ),
            ),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: EdgeInsets.fromLTRB(hPad + 2, 12, hPad, 14),
                  child: const Text(
                    'Settings',
                    style: TextStyle(
                      color: Tokens.text,
                      fontSize: 30,
                      fontWeight: FontWeight.w800,
                      letterSpacing: -0.5,
                    ),
                  ),
                ),
                Padding(
                  padding: EdgeInsets.fromLTRB(hPad, 0, hPad, 16),
                  child: _SearchBox(controller: widget.search),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _SearchBox extends StatelessWidget {
  const _SearchBox({required this.controller});

  final TextEditingController controller;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return GlassSurface(
      radius: Tokens.radiusPill,
      padding: const EdgeInsets.only(left: 16, right: 8),
      child: SizedBox(
        height: 50,
        child: Row(
          children: [
            Icon(Icons.search_rounded, color: accent, size: 20),
            const SizedBox(width: 8),
            Expanded(
              child: TextField(
                controller: controller,
                cursorColor: accent,
                style: const TextStyle(color: Tokens.text, fontSize: 15),
                decoration: const InputDecoration(
                  hintText: 'Search settings...',
                  hintStyle: TextStyle(color: Tokens.dim, fontSize: 15),
                  filled: false,
                  border: InputBorder.none,
                  enabledBorder: InputBorder.none,
                  focusedBorder: InputBorder.none,
                  isDense: true,
                  contentPadding: EdgeInsets.symmetric(
                    horizontal: 2,
                    vertical: 14,
                  ),
                ),
              ),
            ),
            if (controller.text.isNotEmpty)
              IconButton(
                icon: const Icon(Icons.close_rounded, size: 18),
                color: Tokens.dim,
                tooltip: 'Clear',
                constraints: const BoxConstraints.tightFor(
                  width: 36,
                  height: 36,
                ),
                padding: EdgeInsets.zero,
                onPressed: controller.clear,
              ),
          ],
        ),
      ),
    );
  }
}

/// Apple-style grouped hub row with a rounded-square tinted icon badge.
class _HubItem extends StatelessWidget {
  const _HubItem({
    required this.icon,
    required this.badgeColor,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  final IconData icon;
  final Color badgeColor;
  final String title, subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
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
  }
}
