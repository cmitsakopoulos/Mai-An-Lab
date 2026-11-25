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
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(18, 12, 28, 0),
          child: Row(
            children: [
              IconButton(
                icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 16),
                color: accent,
                tooltip: 'Back',
                onPressed: onBack,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 15),
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
    return Stack(
      children: [
        ListView(
          padding: const EdgeInsets.fromLTRB(28, 0, 28, 100),
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
  Widget build(BuildContext context) => Container(
    padding: EdgeInsets.all(padding),
    decoration: BoxDecoration(
      color: Tokens.surface2,
      borderRadius: BorderRadius.circular(radius),
      border: Border.all(color: borderColor),
    ),
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
      Icon(icon, color: Theme.of(context).colorScheme.primary, size: 20),
      const SizedBox(width: 10),
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
  final Widget content = icon == null
      ? Text(text)
      : Row(
          children: [
            Icon(
              icon,
              size: 20,
              color: color ?? Theme.of(m.context).colorScheme.primary,
            ),
            const SizedBox(width: 10),
            Expanded(child: Text(text)),
          ],
        );
  m
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: content));
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

  Widget _item(SettingEntry e, {String? subtitle}) => _HubItem(
    icon: e.icon,
    title: e.title,
    subtitle: subtitle ?? e.subtitle,
    onTap: () => widget.onOpen(e.page),
  );

  @override
  Widget build(BuildContext context) {
    final query = widget.search.text.trim().toLowerCase();
    final children = <Widget>[
      const Text(
        'Settings',
        style: TextStyle(
          color: Tokens.text,
          fontSize: 32,
          fontWeight: FontWeight.w900,
        ),
      ),
      const SizedBox(height: 12),
      _SearchBox(controller: widget.search),
      const SizedBox(height: 16),
    ];
    if (query.isEmpty) {
      Widget section(String label) => Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: AccentLabel(label, size: 11),
      );
      const divider = Divider(color: Tokens.border, height: 30);
      children.addAll([
        section('GENERAL'),
        if (widget.hasSync) _item(_entry(SettingsPage.companion)),
        _item(_entry(SettingsPage.storage)),
        _item(_entry(SettingsPage.account)),
        _item(_entry(SettingsPage.assistant)),
        divider,
        section('CUSTOMISATION'),
        _item(_entry(SettingsPage.appearance)),
        _item(_entry(SettingsPage.audio)),
        _item(_entry(SettingsPage.haptics)),
        if (widget.hasWorkbench) ...[
          divider,
          section('LIBRARY INTELLIGENCE'),
          _item(_entry(SettingsPage.metadata)),
        ],
        divider,
        section('DEVELOPER TOOLS'),
        _item(_entry(SettingsPage.permissions)),
        _item(_entry(SettingsPage.database)),
        _item(_entry(SettingsPage.advanced)),
        divider,
        _item(_entry(SettingsPage.about)),
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
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: AccentLabel('SEARCH RESULTS (${matches.length})', size: 11),
          ),
        );
        for (final m in matches) {
          children.add(_item(m, subtitle: '[${m.category}] ${m.subtitle}'));
        }
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
    return ListView(
      padding: const EdgeInsets.fromLTRB(28, 20, 28, 35),
      children: children,
    );
  }
}

class _SearchBox extends StatelessWidget {
  const _SearchBox({required this.controller});

  final TextEditingController controller;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    return Container(
      padding: const EdgeInsets.only(left: 12, right: 6),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Tokens.border, width: 1.5),
      ),
      child: Row(
        children: [
          Icon(Icons.search_rounded, color: accent, size: 18),
          const SizedBox(width: 4),
          Expanded(
            child: TextField(
              controller: controller,
              cursorColor: accent,
              style: const TextStyle(color: Tokens.text, fontSize: 13),
              decoration: const InputDecoration(
                hintText: 'Search settings...',
                hintStyle: TextStyle(color: Tokens.dim, fontSize: 13),
                filled: false,
                border: InputBorder.none,
                contentPadding: EdgeInsets.symmetric(
                  horizontal: 4,
                  vertical: 12,
                ),
              ),
            ),
          ),
          if (controller.text.isNotEmpty)
            IconButton(
              icon: const Icon(Icons.close, size: 16),
              color: Tokens.dim,
              tooltip: 'Clear',
              onPressed: controller.clear,
            ),
        ],
      ),
    );
  }
}

/// ui/widgets.py `HubSettingItem`.
class _HubItem extends StatelessWidget {
  const _HubItem({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  final IconData icon;
  final String title, subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Material(
        color: Tokens.surface,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(Tokens.radiusCard),
          side: const BorderSide(color: Tokens.borderSubtle),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            child: Row(
              children: [
                Icon(
                  icon,
                  color: Theme.of(context).colorScheme.primary,
                  size: 22,
                ),
                const SizedBox(width: 16),
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
                      Dim(subtitle),
                    ],
                  ),
                ),
                const SizedBox(width: 16),
                Icon(
                  Icons.chevron_right_rounded,
                  color: Tokens.dim.withValues(alpha: 0.4),
                  size: 18,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
