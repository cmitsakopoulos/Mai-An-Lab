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
import 'settings_kit.dart';
import 'settings_search.dart';
import 'sync_hub.dart';
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

  /// The anchor the open page brings into view and pulses (a row-level
  /// search result); null otherwise.
  String? _target;

  void open(SettingsPage p) => setState(() {
    page = p;
    _target = null;
  });

  /// A row-level search result: its page, scrolled to the row.
  void openRow(SettingRow r) => setState(() {
    page = r.page;
    _target = r.id;
  });

  /// `_show_hub`, keeping the search.
  void showHub() => setState(() {
    page = null;
    _target = null;
  });

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
                    onOpenRow: openRow,
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
    return SettingsTarget(
      id: _target,
      child: SubpageFrame(title: p.title, onBack: showHub, child: body),
    );
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

/// A sub-page's scrolling column of [SettingsSection]s.
class GroupBody extends StatelessWidget {
  const GroupBody({super.key, required this.children, this.spacing = 26});

  final List<Widget> children;
  final double spacing;

  @override
  Widget build(BuildContext context) {
    final hPad = Tokens.pagePad(context);
    return ListView(
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

/// `_render_hub_or_search`: the pages in four groups, or, while searching,
/// the matching pages and then the matching rows inside them.
class _Hub extends StatelessWidget {
  const _Hub({
    super.key,
    required this.search,
    required this.onOpen,
    required this.onOpenRow,
    required this.hasSync,
    this.hasWorkbench = false,
    this.highlight,
  });

  final TextEditingController search;
  final ValueChanged<SettingsPage> onOpen;
  final ValueChanged<SettingRow> onOpenRow;
  final bool hasSync;
  final bool hasWorkbench;

  /// Scrolled to and pulsed once (the header gear's entry).
  final SettingsPage? highlight;

  static String _anchorOf(SettingsPage p) => 'page:${p.name}';

  static SettingEntry _entry(SettingsPage p) =>
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

  bool _has(SettingsPage p) =>
      (hasSync || p != SettingsPage.companion) &&
      (hasWorkbench || p != SettingsPage.metadata);

  Widget _group(
    BuildContext context,
    String label,
    List<SettingEntry> entries, {
    bool searching = false,
  }) {
    final accent = Theme.of(context).colorScheme.primary;
    return SettingsSection(
      header: label,
      children: [
        for (final e in entries)
          SettingsAnchor(
            id: searching ? 'search:${e.page.name}' : _anchorOf(e.page),
            child: SettingsTile.nav(
              leading: SettingsIcon(e.icon, color: _badgeColor(e.page, accent)),
              title: e.title,
              subtitle: searching
                  ? '[${e.category}] ${e.subtitle}'
                  : e.subtitle,
              onTap: () => onOpen(e.page),
            ),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: search,
    builder: (context, _) => _build(context),
  );

  Widget _build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final query = search.text.trim().toLowerCase();
    final hPad = Tokens.pagePad(context);
    final children = <Widget>[];
    if (query.isEmpty) {
      children.addAll([
        _group(context, 'Look & Feel', [
          _entry(SettingsPage.appearance),
          _entry(SettingsPage.audio),
          _entry(SettingsPage.haptics),
        ]),
        _group(context, 'Library & Sync', [
          _entry(SettingsPage.storage),
          if (hasWorkbench) _entry(SettingsPage.metadata),
          if (hasSync) _entry(SettingsPage.companion),
        ]),
        _group(context, 'Services & AI', [
          _entry(SettingsPage.account),
          _entry(SettingsPage.assistant),
        ]),
        _group(context, 'System', [
          _entry(SettingsPage.permissions),
          _entry(SettingsPage.database),
          _entry(SettingsPage.advanced),
          _entry(SettingsPage.about),
        ]),
      ]);
    } else {
      final matches = [
        for (final e in searchSettings(query))
          if (_has(e.page)) e,
      ];
      final rows = [
        for (final r in searchSettingRows(query))
          if (_has(r.page)) r,
      ];
      if (matches.isNotEmpty) {
        children.add(
          _group(
            context,
            'Search Results (${matches.length})',
            matches,
            searching: true,
          ),
        );
      }
      if (rows.isNotEmpty) {
        children.add(
          SettingsSection(
            header: 'Settings (${rows.length})',
            children: [
              for (final r in rows)
                SettingsTile.nav(
                  leading: SettingsIcon(
                    _entry(r.page).icon,
                    color: _badgeColor(r.page, accent),
                  ),
                  title: r.title,
                  subtitle: r.page.title,
                  onTap: () => onOpenRow(r),
                ),
            ],
          ),
        );
      }
      if (matches.isEmpty && rows.isEmpty) {
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
                  onTap: search.clear,
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
            controller: search,
            hintText: 'Search settings…',
            clearTooltip: 'Clear',
          ),
        ),
      ],
      child: SettingsTarget(
        id: highlight == null ? null : _anchorOf(highlight!),
        child: Builder(
          builder: (context) => ListView(
            padding: EdgeInsets.fromLTRB(
              hPad,
              HeaderInsets.topOf(context) + 8,
              hPad,
              32,
            ),
            children: [
              for (final (i, c) in children.indexed) ...[
                if (i > 0) const SizedBox(height: 22),
                c,
              ],
            ],
          ),
        ),
      ),
    );
  }
}
