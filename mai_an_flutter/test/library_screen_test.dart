// The Library screen lays out every view on a phone-sized screen without
// overflow, and the view bar, sort menu and tree rows respond.
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/library/library_screen.dart';
import 'package:mai_an_lab/library/track_query.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/ui/tokens.dart';
import 'package:sqlite3/sqlite3.dart';

import 'fixtures.dart';

void main() {
  late Directory dir;
  late String path;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('mai_an_screen');
    path = '${dir.path}/library.db';
    final db = sqlite3.open(path);
    createLibrarySchema(db);
    db.execute('''
      INSERT INTO artists (id, name, album_count, track_count) VALUES
        (1, 'An Artist Whose Name Is Long Enough To Wrap Onto Another Line', 1, 2);
      INSERT INTO albums (id, artist_id, title, genre_bucket, track_count) VALUES
        (1, 1, 'A Very Long Album Title That Goes On And On And On', 'Electronic', 2);
      INSERT INTO tracks (id, album_id, title, track_num, path, added_date, duration) VALUES
        (1, 1, 'First', 1, '/m/1.flac', 100, 200),
        (2, 1, 'Second', 2, '/m/2.mp3', 200, 61);
    ''');
    db.close();
  });

  tearDown(() => dir.deleteSync(recursive: true));

  testWidgets('every view lays out; rows open onto their children', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);

    final lib = LibraryController(() => LibraryRepository.open(path));
    final browse = BrowseController(lib, views: LibraryView.values);
    final player = ValueNotifier<PlayerController?>(null);
    await tester.runAsync(lib.load);
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: LibraryScreen(
          controller: lib,
          browse: browse,
          player: player,
          haptics: Haptics(const HapticsConfig(enabled: false)),
        ),
      ),
    );
    await tester.pump();
    expect(find.text('First'), findsOneWidget);
    expect(find.textContaining('2 / 2 tracks'), findsOneWidget);

    Future<void> settle() async {
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 50)),
      );
      await tester.pumpAndSettle();
    }

    await tester.tap(find.text('Genres'));
    await settle();
    expect(find.text('Electronic'), findsOneWidget);
    expect(find.textContaining('1 / 1 genres'), findsOneWidget);
    await tester.tap(find.text('Electronic'));
    await settle();
    await tester.tap(find.textContaining('An Artist Whose'));
    await settle();
    await tester.tap(find.textContaining('A Very Long Album'));
    await settle();
    expect(find.text('Second'), findsOneWidget);

    await tester.tap(find.text('Albums'));
    await settle();
    expect(find.textContaining('A Very Long Album'), findsOneWidget);
    expect(find.text('First'), findsNothing); // expansions don't carry over

    await tester.tap(find.byIcon(Icons.sort));
    await settle();
    expect(find.text('Date Added'), findsOneWidget);
    await tester.tap(find.text('Album (A–Z)'), warnIfMissed: false);
    await settle();
    expect(lib.sort, LibrarySort.album);

    await tester.tap(find.text('Artists'));
    await settle();
    await tester.tap(find.byIcon(Icons.sort));
    await settle();
    expect(find.text('Most Albums'), findsOneWidget);
    await tester.tapAt(const Offset(10, 700));
    await settle();

    await tester.enterText(find.byType(TextField), 'second');
    await settle();
    expect(find.textContaining('An Artist Whose'), findsOneWidget);

    browse.dispose();
    lib.dispose();
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
  });

  testWidgets('track menu, selection, playlists and delete', (tester) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);

    // Real files, so Delete Track has something to delete.
    final music = Directory('${dir.path}/music')..createSync();
    final first = File('${music.path}/1.flac')..writeAsStringSync('x');
    final second = File('${music.path}/2.mp3')..writeAsStringSync('x');
    final db = sqlite3.open(path);
    db.execute('UPDATE tracks SET path = ? WHERE id = 1', [first.path]);
    db.execute('UPDATE tracks SET path = ? WHERE id = 2', [second.path]);
    db.close();

    final lib = LibraryController(
      () => LibraryRepository.open(path, writable: true),
    );
    final browse = BrowseController(
      lib,
      views: const [LibraryView.playlists, LibraryView.tracks],
    );
    final player = ValueNotifier<PlayerController?>(null);
    await tester.runAsync(lib.load);
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: LibraryScreen(
          controller: lib,
          browse: browse,
          player: player,
          haptics: Haptics(const HapticsConfig(enabled: false)),
        ),
      ),
    );
    Future<void> settle() async {
      for (var i = 0; i < 4; i++) {
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 40)),
        );
        await tester.pumpAndSettle();
      }
    }

    await settle();

    // The full menu; queue actions wait for the player.
    await tester.longPress(find.text('First'));
    await settle();
    for (final item in [
      'Select…',
      'Play Next',
      'Add to Queue',
      'Start Radio',
      'Add to Playlist',
      'Delete Track',
    ]) {
      expect(find.text(item), findsOneWidget, reason: item);
    }
    expect(
      tester
          .widget<ListTile>(find.widgetWithText(ListTile, 'Play Next'))
          .enabled,
      isFalse,
    );

    // Add to Playlist → New Playlist.
    await tester.tap(find.text('Add to Playlist'));
    await settle();
    await tester.tap(find.text('New Playlist'));
    await settle();
    await tester.enterText(find.byType(TextField).last, 'Mix');
    await tester.tap(find.widgetWithText(FilledButton, 'Create'));
    await settle();
    expect(find.text('Customize Playlist'), findsNothing);

    // Select… → the bar; tapping rows ticks them.
    await tester.longPress(find.text('Second'));
    await settle();
    await tester.tap(find.text('Select…'));
    await settle();
    expect(find.text('1 selected'), findsOneWidget);
    await tester.tap(find.text('First'));
    await tester.pump();
    expect(find.text('2 selected'), findsOneWidget);
    await tester.tap(find.byTooltip('Add to Playlist'));
    await settle();
    await tester.tap(find.text('Mix'));
    await settle();
    expect(find.text('Added 2 tracks to Mix.'), findsOneWidget);
    expect(find.textContaining('selected'), findsNothing);

    // The playlist, in its own tab.
    await tester.tap(find.text('Playlists'));
    await settle();
    expect(find.text('2 tracks'), findsOneWidget);
    await tester.tap(find.text('Mix'));
    await settle();
    expect(find.byTooltip('Remove from Playlist'), findsNWidgets(2));
    await tester.tap(find.byTooltip('Remove from Playlist').first);
    await settle();
    expect(find.text('1 track'), findsOneWidget);

    // Delete Track removes the file and the row.
    await tester.tap(find.text('Tracks'));
    await settle();
    await tester.longPress(find.text('Second'));
    await settle();
    await tester.tap(find.text('Delete Track'));
    await settle();
    expect(find.text('Delete Track?'), findsOneWidget);
    await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
    await settle();
    expect(second.existsSync(), isFalse);
    expect(find.text('Second'), findsNothing);
    expect(find.text('Track deleted permanently.'), findsOneWidget);

    browse.dispose();
    lib.dispose();
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
  });
}
