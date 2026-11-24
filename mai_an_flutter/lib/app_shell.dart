import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'assistant/assistant_controller.dart';
import 'assistant/assistant_screen.dart';
import 'core/haptics.dart';
import 'core/notification_router.dart';
import 'downloads/download_dock.dart';
import 'downloads/download_queue.dart';
import 'library/browse_controller.dart';
import 'library/library_controller.dart';
import 'library/library_screen.dart';
import 'library/selection.dart';
import 'player/player_controller.dart';
import 'player/ui/mini_player.dart';
import 'search/landing_controller.dart';
import 'search/search_screen.dart';
import 'settings/settings_page.dart';
import 'ui/tokens.dart';

const _appChannel = MethodChannel('mai_an/app');

/// The main.py tabs the port has. Settings is a pushed page.
enum AppTab {
  jarvis,
  search,
  library;

  /// main.py `_build_ui`: `general.startup_page`, default Library.
  /// When Jarvis is disabled (`appearance.show_jarvis = false`), Jarvis falls
  /// back to Library, as in Flet.
  static AppTab fromStartupPage(String name, {bool showJarvis = true}) =>
      switch (name) {
        'Jarvis' => showJarvis ? jarvis : library,
        'Search' => search,
        _ => library,
      };
}

/// main.py `_build_ui`: the tab content, the mini player and the tab bar,
/// and `navigate_back` for the tabs the port has.
class AppShell extends StatefulWidget {
  const AppShell({
    super.key,
    required this.library,
    required this.browse,
    required this.landing,
    required this.player,
    required this.haptics,
    this.assistant,
    this.showJarvis = false,
    this.unifiedPills = false,
    this.startTab = AppTab.library,
    this.settings,
    this.router,
    this.downloads,
  });

  final LibraryController library;
  final BrowseController browse;
  final LandingController landing;
  final ValueListenable<PlayerController?> player;
  final Haptics haptics;
  final AssistantController? assistant;
  final bool showJarvis;
  final bool unifiedPills;
  final AppTab startTab;

  /// What the Settings page acts on; null leaves its buttons out (tests).
  final SettingsDeps? settings;

  /// Notification taps: the shell's screens register their targets.
  final NotificationRouter? router;

  /// The download queue: the dock above the mini player.
  final DownloadQueue? downloads;

  @override
  State<AppShell> createState() => AppShellState();
}

@visibleForTesting
class AppShellState extends State<AppShell> {
  late AppTab tab = _effectiveTab(widget.startTab);

  bool get _hasJarvis => widget.showJarvis && widget.assistant != null;

  AppTab _effectiveTab(AppTab requested) =>
      requested == AppTab.jarvis && !_hasJarvis ? AppTab.library : requested;

  /// The Library's multi-select: here, because back leaves it first.
  final selection = SelectionController();

  @override
  void didUpdateWidget(AppShell oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (tab == AppTab.jarvis && !_hasJarvis) {
      widget.assistant?.onHide();
      tab = AppTab.library;
      _tabShown(tab);
    }
  }

  @override
  void initState() {
    super.initState();
    _tabShown(tab);
    // library.py's "n tracks not analysed yet" card opens the Library
    // (its Analyse button waits for decision B).
    widget.router?.register('library', (_) => switchTab(AppTab.library));
    if (widget.downloads case final q?) {
      widget.router?.register(
        'downloads',
        (action) => _openDownloads(q, action),
      );
    }
    if (widget.settings?.sync != null) {
      widget.router?.register('sync', _openSync);
    }
  }

  /// main.py `_on_notification_navigate`, "sync": Settings → Companion &
  /// Sync, and for "transfers" (the background card) the dialog for what
  /// the Mac sent.
  void _openSync(String action) {
    final deps = widget.settings!;
    WidgetsBinding.instance
      ..addPostFrameCallback((_) {
        if (!mounted) return;
        openSettings(context, deps, initial: SettingsPage.companion);
        if (action == 'transfers') {
          deps.sync!.checkPendingTransfers(promptUser: true).ignore();
        }
      })
      ..ensureVisualUpdate();
  }

  /// main.py `_on_notification_navigate`, "downloads": the dock's sheet,
  /// and for the summary card's Retry, the last batch's failures again.
  void _openDownloads(DownloadQueue q, String action) {
    // A cold-start tap arrives from initState, before there is a frame.
    WidgetsBinding.instance
      ..addPostFrameCallback((_) {
        if (mounted) _openDownloadsNow(q, action);
      })
      ..ensureVisualUpdate();
  }

  void _openDownloadsNow(DownloadQueue q, String action) {
    showDownloadSheet(context, q, _snack);
    if (action == 'retry') {
      final n = q.retryFailed();
      _snack(
        n > 0
            ? 'Retrying $n download${n != 1 ? 's' : ''}.'
            : 'Nothing to retry. The failed downloads are no longer in the '
                  'list.',
      );
    }
  }

  @override
  void dispose() {
    widget.router?.unregister('library');
    if (widget.downloads != null) widget.router?.unregister('downloads');
    if (widget.settings?.sync != null) widget.router?.unregister('sync');
    selection.dispose();
    super.dispose();
  }

  /// main.py `_switch_tab`.
  void switchTab(AppTab to) {
    final target = _effectiveTab(to);
    if (target == tab) return;
    final from = tab;
    setState(() => tab = target);
    if (from == AppTab.jarvis) widget.assistant?.onHide();
    _tabShown(target);
  }

  /// The tab's `on_show`.
  void _tabShown(AppTab t) {
    if (t == AppTab.search) widget.landing.onShow();
    if (t == AppTab.jarvis) widget.assistant?.onShow().ignore();
  }

  void _snack(String text) {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  /// main.py `play_track(path)` with no source: the Library's tracks view,
  /// as `_play_track_core` resolves it.
  void _playPath(String path) {
    final player = widget.player.value;
    if (player == null) {
      _snack('The player is still starting.');
      return;
    }
    final lib = widget.library;
    final track = lib.allTracks.where((t) => t.path == path).firstOrNull;
    if (track == null) {
      _snack('Track not found in current view.');
      return;
    }
    player.playTrack(
      track,
      context: lib.visible,
      generalView: lib.isGeneralView && !lib.closestMatch,
    );
  }

  /// search.py `_open_in_library` + library.py `show_filtered`: the Library
  /// on [view] with [query] in its search box. A hidden view falls back to
  /// Tracks (or the first shown view); an album or artist name still finds
  /// its tracks there, other names are dropped.
  void openInLibrary(LibraryView view, String query) {
    final browse = widget.browse;
    switchTab(AppTab.library);
    if (!browse.views.contains(view)) {
      final fallback = browse.views.contains(LibraryView.tracks)
          ? LibraryView.tracks
          : browse.views.first;
      if (!(view == LibraryView.albums || view == LibraryView.artists) ||
          fallback != LibraryView.tracks) {
        query = '';
      }
      _snack(
        'The ${view.label} tab is hidden (Settings → Appearance). '
        'Showing ${fallback.name} instead.',
      );
      view = fallback;
    }
    widget.library.setQuery(query);
    browse.setView(view);
  }

  /// main.py `switch_to_settings(subpage)`.
  void Function([SettingsPage?])? get _openSettings {
    final deps = widget.settings;
    if (deps == null) return null;
    return ([SettingsPage? page]) => openSettings(context, deps, initial: page);
  }

  @override
  Widget build(BuildContext context) {
    // Back on a main tab sends the app to the background (like Home)
    // instead of finishing it; playback carries on either way.
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        // Selection mode is the innermost state: back leaves it first.
        if (tab == AppTab.library && selection.active) {
          selection.exit();
          return;
        }
        _appChannel.invokeMethod('moveTaskToBack').ignore();
      },
      child: Scaffold(
        body: Stack(
          fit: StackFit.expand,
          children: [
            if (_hasJarvis)
              _FadeTab(
                active: tab == AppTab.jarvis,
                child: AssistantScreen(
                  controller: widget.assistant!,
                  onOpenSettings: _openSettings,
                ),
              ),
            _FadeTab(
              active: tab == AppTab.search,
              child: SearchScreen(
                landing: widget.landing,
                onPlayTrack: _playPath,
                onOpenInLibrary: openInLibrary,
                onOpenSettings: _openSettings,
              ),
            ),
            _FadeTab(
              active: tab == AppTab.library,
              child: LibraryScreen(
                controller: widget.library,
                browse: widget.browse,
                unifiedPills: widget.unifiedPills,
                player: widget.player,
                haptics: widget.haptics,
                selection: selection,
                onOpenSettings: _openSettings,
                sync: widget.settings?.sync,
              ),
            ),
          ],
        ),
        bottomNavigationBar: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (widget.downloads case final q?)
              DownloadPill(queue: q, onSnack: _snack),
            ValueListenableBuilder(
              valueListenable: widget.player,
              builder: (context, player, _) => player == null
                  ? const SizedBox.shrink()
                  : MiniPlayer(player: player),
            ),
            _TabBar(tab: tab, showJarvis: _hasJarvis, onSelect: switchTab),
          ],
        ),
      ),
    );
  }
}

/// main.py `_nav`: no pill, a hairline on top, labels always shown, the
/// accent indicator at about a third.
class _TabBar extends StatelessWidget {
  const _TabBar({
    required this.tab,
    required this.showJarvis,
    required this.onSelect,
  });

  final AppTab tab;
  final bool showJarvis;
  final ValueChanged<AppTab> onSelect;

  @override
  Widget build(BuildContext context) {
    final accent = Theme.of(context).colorScheme.primary;
    final tabs = [if (showJarvis) AppTab.jarvis, AppTab.search, AppTab.library];
    final selectedIndex = tabs.indexOf(tab).clamp(0, tabs.length - 1);
    return DecoratedBox(
      decoration: const BoxDecoration(
        border: Border(top: BorderSide(color: Tokens.borderSubtle, width: 0.5)),
      ),
      child: NavigationBar(
        selectedIndex: selectedIndex,
        onDestinationSelected: (i) => onSelect(tabs[i]),
        backgroundColor: Tokens.surface,
        indicatorColor: accent.withValues(alpha: 0x55 / 0xFF),
        elevation: 0,
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        labelTextStyle: const WidgetStatePropertyAll(
          TextStyle(fontSize: 11, fontWeight: FontWeight.w500),
        ),
        destinations: [
          if (showJarvis)
            const NavigationDestination(
              icon: Icon(Icons.auto_awesome_rounded),
              selectedIcon: Icon(Icons.auto_awesome_rounded),
              label: 'Jarvis',
            ),
          const NavigationDestination(
            icon: Icon(Icons.search_rounded),
            label: 'Search',
          ),
          const NavigationDestination(
            icon: Icon(Icons.library_music_outlined),
            selectedIcon: Icon(Icons.library_music_rounded),
            label: 'Library',
          ),
        ],
      ),
    );
  }
}

/// main.py `_tab_content`: a fade between tabs (220 ms in, 160 ms out). The
/// hidden tab keeps its state (Flet's `_view_cache`) and goes offstage once
/// faded out.
class _FadeTab extends StatefulWidget {
  const _FadeTab({required this.active, required this.child});

  final bool active;
  final Widget child;

  @override
  State<_FadeTab> createState() => _FadeTabState();
}

class _FadeTabState extends State<_FadeTab> {
  late bool _offstage = !widget.active;

  @override
  void didUpdateWidget(_FadeTab old) {
    super.didUpdateWidget(old);
    if (widget.active) _offstage = false;
  }

  @override
  Widget build(BuildContext context) {
    final active = widget.active;
    return Offstage(
      offstage: _offstage,
      child: IgnorePointer(
        ignoring: !active,
        child: AnimatedOpacity(
          opacity: active ? 1 : 0,
          duration: Duration(milliseconds: active ? 220 : 160),
          curve: active ? Curves.easeOutQuad : Curves.easeInQuad,
          onEnd: () {
            if (!widget.active && mounted) {
              setState(() => _offstage = true);
            }
          },
          // The fade's own ticker sits outside, so it can finish.
          child: TickerMode(enabled: active, child: widget.child),
        ),
      ),
    );
  }
}
