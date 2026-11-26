import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'analysis/library_analysis.dart';
import 'assistant/assistant_controller.dart';
import 'assistant/assistant_screen.dart';
import 'core/haptics.dart';
import 'core/notification_router.dart';
import 'data/library_repository.dart';
import 'downloads/download_dock.dart';
import 'downloads/download_queue.dart';
import 'library/browse_controller.dart';
import 'library/library_controller.dart';
import 'library/library_screen.dart';
import 'library/selection.dart';
import 'onboarding/onboarding.dart';
import 'player/player_controller.dart';
import 'player/ui/mini_player.dart';
import 'search/landing_controller.dart';
import 'search/search_controller.dart';
import 'search/search_history.dart';
import 'search/search_screen.dart';
import 'settings/settings_page.dart';
import 'ui/activity_pill.dart';
import 'ui/frost.dart';
import 'ui/keyboard_guard.dart';
import 'ui/size_reporter.dart';
import 'ui/sliding_segmented.dart';
import 'ui/toast.dart';
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
    this.searchController,
    this.searchHistory,
    this.onboarding,
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

  /// Full Search tab controller (Qobuz/Deezer remote search, previews, multi-select).
  final SearchTabController? searchController;

  /// Persisted recent search queries (`recent_searches.json`).
  final SearchHistoryStore? searchHistory;

  /// main.py `show_onboarding_guide`: the first-run wizard, opened over the
  /// shell after its first frame. Null when the install is set up.
  final OnboardingFlow? onboarding;

  @override
  State<AppShell> createState() => AppShellState();
}

@visibleForTesting
class AppShellState extends State<AppShell> {
  late AppTab tab = _effectiveTab(widget.startTab);

  /// The bottom dock's real height, measured each layout ([ShellInsets]).
  final _dockHeight = ValueNotifier<double?>(null);

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
      dropTextFocus();
      tab = AppTab.library;
      _tabShown(tab);
    }
  }

  @override
  void initState() {
    super.initState();
    _tabShown(tab);
    // main.py "library": library.py's "n tracks not analysed yet" card opens
    // the Library; its Analyse button ("library:analyse") also starts the
    // on-phone analysis.
    widget.router?.register('library', (action) {
      switchTab(AppTab.library);
      if (action == 'analyse') widget.library.analyseLibrary().ignore();
    });
    if (widget.downloads case final q?) {
      widget.router?.register(
        'downloads',
        (action) => _openDownloads(q, action),
      );
    }
    if (widget.settings?.sync != null) {
      widget.router?.register('sync', _openSync);
    }
    if (widget.settings?.workbench != null) {
      widget.router?.register('workbench', _openWorkbenchRoute);
    }
    if (widget.settings != null) {
      widget.router?.register('audio', _openAudioRoute);
    }
    if (widget.onboarding case final flow?) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _onboard(flow));
    }
  }

  /// `_finish_onboarding`'s `restart_ui(target_tab, post_message)`: the
  /// chosen accent and views apply as config.toml changes; this lands on
  /// the chosen tab, welcomes, and indexes the chosen folder.
  Future<void> _onboard(OnboardingFlow flow) async {
    if (!mounted) return;
    final done = await flow(context);
    if (done == null || !mounted) return;
    final (startup, _) = done;
    switchTab(AppTab.fromStartupPage(startup));
    _snack(kOnboardingWelcome);
    unawaited(widget.library.scan());
  }

  /// main.py `_on_notification_navigate`, "workbench": Settings → Metadata,
  /// and for the cards' buttons, Sync again ("sync") or the suggestions
  /// selected ("suggestions").
  void _openWorkbenchRoute(String action) {
    WidgetsBinding.instance
      ..addPostFrameCallback((_) {
        if (!mounted) return;
        final w = widget.settings!.workbench!;
        openSettings(context, widget.settings!, initial: SettingsPage.metadata);
        if (action == 'sync') {
          w.startSync().ignore();
        } else if (action == 'suggestions') {
          // After the page's own reload, which would clear the selection's
          // source rows.
          w.reload().then((_) => w.reviewSuggestions()).ignore();
        }
      })
      ..ensureVisualUpdate();
  }

  /// main.py `open_artist_metadata_editor` / `open_genre_metadata_workbench`.
  void _openWorkbench({String? artist, String? genre}) {
    final deps = widget.settings!;
    final w = deps.workbench!;
    openSettings(context, deps, initial: SettingsPage.metadata);
    if (artist != null) {
      w.focusArtist(artist).ignore();
    } else {
      w.filterGenre(genre ?? '').ignore();
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
    _dockHeight.dispose();
    widget.router?.unregister('library');
    if (widget.downloads != null) widget.router?.unregister('downloads');
    if (widget.settings?.sync != null) widget.router?.unregister('sync');
    if (widget.settings?.workbench != null) {
      widget.router?.unregister('workbench');
    }
    if (widget.settings != null) {
      widget.router?.unregister('audio');
    }
    selection.dispose();
    super.dispose();
  }

  /// main.py `_switch_tab`.
  void switchTab(AppTab to) {
    final target = _effectiveTab(to);
    if (target == tab) return;
    final from = tab;
    // The leaving tab stays mounted offstage; a search field there would
    // otherwise keep focus (and later be handed it back) with the keyboard up.
    dropTextFocus();
    setState(() => tab = target);
    if (from == AppTab.jarvis) widget.assistant?.onHide();
    _tabShown(target);
  }

  /// The tab's `on_show`.
  void _tabShown(AppTab t) {
    if (t == AppTab.search) widget.landing.onShow();
    if (t == AppTab.jarvis) widget.assistant?.onShow().ignore();
  }

  void _openAudioRoute(String action) {
    WidgetsBinding.instance
      ..addPostFrameCallback((_) {
        if (!mounted || widget.settings == null) return;
        openSettings(context, widget.settings!, initial: SettingsPage.audio);
      })
      ..ensureVisualUpdate();
  }

  void _snack(String text) {
    if (!mounted) return;
    showAppToast(context, text);
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

  /// library.py `_redownload`: switch to Search tab on Tracks with `"<title> <artist>"`.
  void _redownloadTrack(Track t) {
    final parts = <String>[
      if (t.title.trim().isNotEmpty) t.title.trim(),
      if (t.artist.trim().isNotEmpty && t.artist.trim() != 'Unknown')
        t.artist.trim(),
    ];
    final query = parts.join(' ').trim();
    if (query.isEmpty) return;
    switchTab(AppTab.search);
    final sc = widget.searchController;
    if (sc != null) {
      unawaited(sc.startRedownloadSearch(query));
    }
  }

  /// main.py `switch_to_settings(subpage)`.
  void Function([SettingsPage?])? get _openSettings {
    final deps = widget.settings;
    if (deps == null) return null;
    return ([SettingsPage? page]) => openSettings(context, deps, initial: page);
  }

  /// Opens Settings from a tab that has a primary relevant hub entry (e.g. AI
  /// Assistant or Search Authentication): direct subpage calls still jump in,
  /// while the header gear icon opens the hub scrolled to & highlighting [entry].
  void Function([SettingsPage?])? _openSettingsHighlighting(
    SettingsPage entry,
  ) {
    final deps = widget.settings;
    if (deps == null) return null;
    return ([SettingsPage? page]) => openSettings(
      context,
      deps,
      initial: page,
      highlight: page == null ? entry : null,
    );
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
        if (tab == AppTab.search &&
            (widget.searchController?.selectionMode ?? false)) {
          widget.searchController!.exitSelection();
          return;
        }
        _appChannel.invokeMethod('moveTaskToBack').ignore();
      },
      child: Scaffold(
        extendBody: true,
        body: _ShellInsetsProvider(
          dockHeight: _dockHeight,
          player: widget.player,
          downloads: widget.downloads,
          analysis: widget.library.analysis,
          child: Stack(
            fit: StackFit.expand,
            children: [
              if (_hasJarvis)
                _FadeTab(
                  active: tab == AppTab.jarvis,
                  child: AssistantScreen(
                    controller: widget.assistant!,
                    onOpenSettings: _openSettingsHighlighting(
                      SettingsPage.assistant,
                    ),
                  ),
                ),
              _FadeTab(
                active: tab == AppTab.search,
                child: SearchScreen(
                  landing: widget.landing,
                  controller: widget.searchController,
                  downloads: widget.downloads,
                  unifiedPills: widget.unifiedPills,
                  onPlayTrack: _playPath,
                  onOpenInLibrary: openInLibrary,
                  searchHistory: widget.searchHistory,
                  onOpenSettings: _openSettingsHighlighting(
                    SettingsPage.account,
                  ),
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
                  onRedownloadTrack: widget.searchController == null
                      ? null
                      : _redownloadTrack,
                  onEditArtist: widget.settings?.workbench == null
                      ? null
                      : (name) => _openWorkbench(artist: name),
                  onCurateGenre: widget.settings?.workbench == null
                      ? null
                      : (name) => _openWorkbench(genre: name),
                ),
              ),
            ],
          ),
        ),
        bottomNavigationBar: MediaQuery.viewInsetsOf(context).bottom > 0
            ? const SizedBox.shrink()
            : SizeReporter(
                onSize: (s) => _dockHeight.value = s.height,
                child: BackdropGroup(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      ActivityPill(
                        analysis: widget.library.analysis,
                        downloads: widget.downloads,
                        onSnack: _snack,
                        onOpenAudioSettings: widget.settings == null
                            ? null
                            : () => _openSettings?.call(SettingsPage.audio),
                        grouped: true,
                      ),
                      ValueListenableBuilder(
                        valueListenable: widget.player,
                        builder: (context, player, _) => player == null
                            ? const SizedBox.shrink()
                            : MiniPlayer(player: player, grouped: true),
                      ),
                      _TabBar(
                        tab: tab,
                        showJarvis: _hasJarvis,
                        onSelect: switchTab,
                      ),
                    ],
                  ),
                ),
              ),
      ),
    );
  }
}

/// Computes the live height of the floating bottom pills and shares it via
/// [ShellInsets] so scrollable tabs pad their tails cleanly.
class _ShellInsetsProvider extends StatelessWidget {
  const _ShellInsetsProvider({
    required this.dockHeight,
    required this.player,
    required this.downloads,
    required this.analysis,
    required this.child,
  });

  /// The dock's measured height (tab bar + mini player + activity pill +
  /// gesture bar); null until its first layout, when the estimate is used.
  final ValueListenable<double?> dockHeight;
  final ValueListenable<PlayerController?> player;
  final DownloadQueue? downloads;
  final DspAnalysis analysis;
  final Widget child;

  static const _tabBarHeight = 58.0; // 48 pill + 10 bottom gap
  static const _miniPlayerHeight = 70.0; // ~62 pill + 8 gap
  static const _activityPillHeight = 66.0; // unified ActivityPill + 8 gap

  @override
  Widget build(BuildContext context) {
    final keyboardOpen = MediaQuery.viewInsetsOf(context).bottom > 0;
    if (keyboardOpen) {
      return ShellInsets(bottom: 0, child: child);
    }
    // Use viewPadding (raw OS gesture bar) rather than padding: Scaffold with
    // extendBody: true already inflates MediaQuery.padding.bottom to the full
    // bottomNavigationBar height, which would double-count the bottom pills.
    final safeBottom = MediaQuery.viewPaddingOf(context).bottom;
    return ValueListenableBuilder<double?>(
      valueListenable: dockHeight,
      builder: (context, measured, _) => measured != null
          ? ShellInsets(bottom: measured, child: child)
          : _estimate(safeBottom),
    );
  }

  Widget _estimate(double safeBottom) {
    return ValueListenableBuilder<PlayerController?>(
      valueListenable: player,
      builder: (context, p, _) {
        final listenables = <Listenable>[?p, ?downloads, analysis];
        return ListenableBuilder(
          listenable: Listenable.merge(listenables),
          builder: (context, _) {
            final hasMini =
                p != null && p.hasEverPlayed && p.nowPlaying != null;
            final hasActivity = ActivityPill.isVisible(analysis, downloads);
            final bottom =
                safeBottom +
                _tabBarHeight +
                (hasMini ? _miniPlayerHeight : 0) +
                (hasActivity ? _activityPillHeight : 0);
            return ShellInsets(bottom: bottom, child: child);
          },
        );
      },
    );
  }
}

/// The floating bottom bar: a frosted glass pill hovering over scrolling content.
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
    final hPad = Tokens.dockPad(context);
    final tabs = [if (showJarvis) AppTab.jarvis, AppTab.search, AppTab.library];
    return SafeArea(
      top: false,
      child: Padding(
        padding: EdgeInsets.fromLTRB(hPad, 0, hPad, 10),
        child: SlidingSegmented<AppTab>(
          segments: [
            for (final t in tabs)
              Segment(
                value: t,
                label: switch (t) {
                  AppTab.jarvis => 'Jarvis',
                  AppTab.search => 'Search',
                  AppTab.library => 'Library',
                },
                icon: switch (t) {
                  AppTab.jarvis => Icons.auto_awesome_rounded,
                  AppTab.search => Icons.search_rounded,
                  AppTab.library => Icons.library_music_rounded,
                },
              ),
          ],
          selected: tab,
          onSelect: onSelect,
          accent: accent,
          height: 48,
          fontSize: 12,
          iconSize: 20,
          hPad: 10,
          grouped: true,
        ),
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
        // A hidden tab's fields can't take focus, so a focus restore can
        // never land on one and raise the keyboard.
        child: ExcludeFocus(
          excluding: !active,
          child: AnimatedOpacity(
            opacity: active ? 1 : 0,
            duration: active ? Tokens.motionBase : Tokens.motionFast,
            curve: active ? Tokens.curveIn : Tokens.curveOut,
            onEnd: () {
              if (!widget.active && mounted) {
                setState(() => _offstage = true);
              }
            },
            // The fade's own ticker sits outside, so it can finish.
            child: TickerMode(enabled: active, child: widget.child),
          ),
        ),
      ),
    );
  }
}
