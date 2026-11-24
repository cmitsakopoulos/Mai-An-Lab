// The tab shell and the Search landing (PORT_MAP §10 4g): the startup tab,
// the landing's refresh rule, the cards, and their links into the Library.
// The queries themselves are golden-tested in landing_queries_test.dart.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/app_shell.dart';
import 'package:mai_an_lab/core/app_config.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/search/landing_controller.dart';
import 'package:mai_an_lab/search/search_screen.dart';
import 'package:mai_an_lab/ui/tokens.dart';

const _setup = [
  "INSERT INTO artists (id, name) VALUES (1, 'Björk'), (2, 'Aphex Twin')",
  "INSERT INTO albums (id, artist_id, title, genre) VALUES "
      "(1, 1, 'Post', 'Electronic, Pop'), (2, 2, 'SAW', 'Ambient')",
  "INSERT INTO tracks (album_id, title, track_num, path, added_date, duration) "
      "VALUES (1, 'Hyperballad', 1, '/m/1.flac', 100, 200), "
      "(1, 'Isobel', 2, '/m/2.flac', 200, 200), "
      "(2, 'Xtal', 1, '/m/3.flac', 300, 200)",
  "INSERT INTO play_counts (track_path, count, last_played) VALUES "
      "('/m/1.flac', 12, 5), ('/m/3.flac', 3, 6)",
];

void main() {
  late Directory dir;
  late String path;

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('mai_an_shell');
    path = '${dir.path}/library.db';
    final repo = await LibraryRepository.open(path, writable: true);
    await repo.debugExecute([for (final s in _setup) (s, const [])]);
    await repo.close();
  });
  tearDown(() => dir.deleteSync(recursive: true));

  test('config: startup page and landing switches', () {
    final c = AppConfig.parse('''
[general]
startup_page = "Search"
[landing]
show_top_artists = false
show_library_stats = "false"
show_top_genres = 0
''');
    expect(AppTab.fromStartupPage(c.startupPage), AppTab.search);
    expect(c.landing.mostListened, isTrue);
    expect(c.landing.topArtists, isFalse);
    // Python's bool(): a non-empty string is true.
    expect(c.landing.stats, isTrue);
    expect(c.landing.topGenres, isFalse);
    expect(
      AppTab.fromStartupPage(const AppConfig().startupPage),
      AppTab.library,
    );
    expect(AppTab.fromStartupPage('Jarvis'), AppTab.library);
    expect(AppTab.fromStartupPage('Settings'), AppTab.library);
  });

  test('thousands separators as f"{n:,}"', () {
    expect(thousands(0), '0');
    expect(thousands(999), '999');
    expect(thousands(1216), '1,216');
    expect(thousands(1234567), '1,234,567');
  });

  test(
    'the landing rebuilds on show when empty or over a minute old',
    () async {
      final lib = LibraryController(() => LibraryRepository.open(path));
      var now = DateTime(2026, 11, 24, 12);
      final landing = LandingController(lib, clock: () => now);
      var builds = 0;
      landing.addListener(() => builds++);

      landing.onShow(); // the library isn't open yet: built once it is
      await lib.load();
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(builds, 1);
      expect([for (final t in landing.mostPlayed) t.count], [12, 3]);
      expect(
        [for (final a in landing.topArtists) a.name],
        ['Björk', 'Aphex Twin'],
      );
      expect(
        [for (final g in landing.topGenres) g.name],
        ['Electronic', 'Pop', 'Ambient'],
      );
      expect(landing.counts?.tracks, 3);

      now = now.add(const Duration(seconds: 59));
      landing.onShow();
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(builds, 1);

      now = now.add(const Duration(seconds: 2));
      landing.onShow();
      await Future<void>.delayed(const Duration(milliseconds: 50));
      expect(builds, 2);

      landing.dispose();
      lib.dispose();
      // Let the database close before the temp folder goes.
      await Future<void>.delayed(const Duration(milliseconds: 50));
    },
  );

  testWidgets('landing cards link into the Library', (tester) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);

    final lib = LibraryController(() => LibraryRepository.open(path));
    // Flet's default bar: Albums hidden.
    final browse = BrowseController(
      lib,
      views: LibraryView.fromConfig(const LibraryTabs().visible),
    );
    final landing = LandingController(lib);
    final player = ValueNotifier<PlayerController?>(null);
    await tester.runAsync(lib.load);
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: AppShell(
          library: lib,
          browse: browse,
          landing: landing,
          player: player,
          haptics: Haptics(const HapticsConfig(enabled: false)),
          startTab: AppTab.search,
        ),
      ),
    );
    Future<void> settle() async {
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 50)),
      );
      await tester.pumpAndSettle();
    }

    await settle();
    expect(find.text('Most Listened Tracks'), findsOneWidget);
    expect(find.text('Most Listened Artists'), findsOneWidget);
    expect(find.text('Most Listened Genres'), findsOneWidget);
    expect(find.text('12 plays • 1 tracks'), findsOneWidget);
    expect(find.text('12 plays'), findsOneWidget);

    // The player isn't up: a track row says so.
    await tester.tap(find.text('Hyperballad'));
    await tester.pump();
    expect(find.text('The player is still starting.'), findsOneWidget);

    final cards = find
        .descendant(
          of: find.byType(SearchScreen),
          matching: find.byType(Scrollable),
        )
        .first;
    await tester.scrollUntilVisible(
      find.text('Library at a Glance'),
      200,
      scrollable: cards,
    );
    expect(find.text('3'), findsWidgets); // tracks
    await tester.scrollUntilVisible(
      find.text('3 plays • 1 tracks'),
      -200,
      scrollable: cards,
    );

    // An artist row: the Artists view, filtered to it.
    await tester.tap(find.text('3 plays • 1 tracks'));
    await settle();
    final shell = tester.state<AppShellState>(find.byType(AppShell));
    expect(shell.tab, AppTab.library);
    expect(browse.view, LibraryView.artists);
    expect(lib.query, 'Aphex Twin');
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller?.text,
      'Aphex Twin',
    );

    // Albums is hidden: Tracks instead, unfiltered, with a note.
    await tester.tap(find.text('Search'));
    await settle();
    await tester.scrollUntilVisible(
      find.text('ALBUMS'),
      200,
      scrollable: cards,
    );
    await tester.tap(find.text('ALBUMS'));
    await settle();
    expect(browse.view, LibraryView.tracks);
    expect(lib.query, '');
    expect(
      find.text(
        'The Albums tab is hidden (Settings → Appearance). '
        'Showing tracks instead.',
      ),
      findsOneWidget,
    );

    // Back leaves selection mode before anything else.
    shell.selection.enable(lib.visible.first);
    await tester.pump();
    expect(shell.selection.active, isTrue);
    await tester.binding.handlePopRoute();
    await tester.pump();
    expect(shell.selection.active, isFalse);

    landing.dispose();
    browse.dispose();
    lib.dispose();
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
  });
}
