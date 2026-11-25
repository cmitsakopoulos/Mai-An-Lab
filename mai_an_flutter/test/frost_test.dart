import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/player/ui/art_tint.dart';
import 'package:mai_an_lab/ui/frost.dart';
import 'package:mai_an_lab/ui/sliding_segmented.dart';
import 'package:mai_an_lab/ui/tokens.dart';

Widget _app(Widget child) => MaterialApp(
  theme: Tokens.theme(),
  home: Scaffold(body: Center(child: child)),
);

void main() {
  testWidgets('ArtPalette: missing file is null, a solid cover is its colour', (
    t,
  ) async {
    await t.runAsync(() async {
      expect(await ArtPalette.of('/nonexistent/cover.png'), isNull);
      final dir = Directory.systemTemp.createTempSync('art');
      addTearDown(() => dir.deleteSync(recursive: true));
      final rec = ui.PictureRecorder();
      Canvas(rec).drawRect(
        const Rect.fromLTWH(0, 0, 16, 16),
        Paint()..color = const Color(0xFFD02020),
      );
      final img = await rec.endRecording().toImage(16, 16);
      final png = await img.toByteData(format: ui.ImageByteFormat.png);
      final f = File('${dir.path}/c.png')
        ..writeAsBytesSync(png!.buffer.asUint8List());
      final c = (await ArtPalette.of(f.path))!;
      expect(c.r, greaterThan(0.7));
      expect(c.g, lessThan(0.3));
    });
  });

  testWidgets('SlidingSegmented selects and slides the pill', (t) async {
    var sel = 'a';
    late StateSetter set;
    await t.pumpWidget(
      _app(
        SizedBox(
          width: 300,
          child: StatefulBuilder(
            builder: (_, s) {
              set = s;
              return SlidingSegmented<String>(
                segments: const [
                  Segment(value: 'a', label: 'A'),
                  Segment(value: 'b', label: 'B'),
                  Segment(value: 'c', label: 'C'),
                ],
                selected: sel,
                onSelect: (v) => set(() => sel = v),
                accent: Colors.amber,
              );
            },
          ),
        ),
      ),
    );
    final pill = find.byType(FractionallySizedBox);
    final x0 = t.getTopLeft(pill).dx;
    await t.tap(find.text('C'));
    await t.pump();
    await t.pump(const Duration(milliseconds: 60));
    final mid = t.getTopLeft(pill).dx;
    await t.pumpAndSettle();
    final x1 = t.getTopLeft(pill).dx;
    expect(sel, 'c');
    expect(mid, greaterThan(x0));
    expect(mid, lessThan(x1)); // in flight, not snapped
    expect(x1 - x0, closeTo(2 * 294 / 3, 2));
  });

  testWidgets('SlidingSegmented iconOnly renders icons with tooltips', (
    t,
  ) async {
    var sel = 'tracks';
    await t.pumpWidget(
      _app(
        SizedBox(
          width: 300,
          child: StatefulBuilder(
            builder: (_, set) => SlidingSegmented<String>(
              segments: const [
                Segment(
                  value: 'tracks',
                  label: 'Tracks',
                  icon: Icons.music_note_rounded,
                ),
                Segment(
                  value: 'albums',
                  label: 'Albums',
                  icon: Icons.album_rounded,
                ),
              ],
              selected: sel,
              onSelect: (v) => set(() => sel = v),
              accent: Colors.amber,
              iconOnly: true,
            ),
          ),
        ),
      ),
    );
    expect(find.text('Tracks'), findsNothing);
    expect(find.byIcon(Icons.album_rounded), findsOneWidget);
    expect(find.byTooltip('Albums'), findsOneWidget);
    await t.tap(find.byIcon(Icons.album_rounded));
    await t.pumpAndSettle();
    expect(sel, 'albums');
  });

  testWidgets('GlassSurface and showFrostedSheet apply BackdropFilter', (
    t,
  ) async {
    await t.pumpWidget(
      _app(
        Builder(
          builder: (c) => GlassSurface(
            child: GlassCard(
              onTap: () => showFrostedSheet<void>(
                context: c,
                showDragHandle: true,
                builder: (_) => const SizedBox(height: 120, child: Text('S')),
              ),
              child: const Text('card'),
            ),
          ),
        ),
      ),
    );
    expect(find.byType(BackdropFilter), findsOneWidget);
    await t.tap(find.text('card'));
    await t.pumpAndSettle();
    expect(find.text('S'), findsOneWidget);
    expect(find.byType(BackdropFilter), findsNWidgets(2));
  });

  testWidgets('GlassScope at 100% opacity skips BackdropFilter (solid mode)', (
    t,
  ) async {
    await t.pumpWidget(
      _app(
        const GlassScope(
          opacity: 1.0,
          child: GlassSurface(child: Text('solid')),
        ),
      ),
    );
    expect(find.text('solid'), findsOneWidget);
    expect(find.byType(BackdropFilter), findsNothing);
  });

  testWidgets('ShellInsets propagates bottom clearance', (t) async {
    double? seen;
    await t.pumpWidget(
      _app(
        ShellInsets(
          bottom: 132,
          child: Builder(
            builder: (c) {
              seen = ShellInsets.bottomOf(c);
              return const SizedBox.shrink();
            },
          ),
        ),
      ),
    );
    expect(seen, 132);
  });

  testWidgets(
    'CategoryPalette resolves multi-color vs unified accent and depth stepping',
    (t) async {
      const accent = Color(0xFF00E5FF);
      CategoryPalette? multi;
      CategoryPalette? unified;

      await t.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(accentColor: accent, unifiedCategories: false),
          home: Builder(
            builder: (c) {
              multi = CategoryPalette.of(c);
              return const SizedBox.shrink();
            },
          ),
        ),
      );
      expect(multi!.unified, isFalse);
      expect(multi!.artist, Tokens.libArtist);
      expect(multi!.album, Tokens.libAlbum);
      expect(multi!.track, Tokens.libTrack);
      expect(multi!.playlist, Tokens.libPlaylist);
      expect(multi!.genre, Tokens.libGenre);
      expect(multi!.atDepth(multi!.artist, 1), Tokens.libArtist);

      await t.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(accentColor: accent, unifiedCategories: true),
          home: Builder(
            builder: (c) {
              unified = CategoryPalette.of(c);
              return const SizedBox.shrink();
            },
          ),
        ),
      );
      await t.pumpAndSettle();
      expect(unified!.unified, isTrue);
      expect(unified!.artist, accent);
      expect(unified!.album, accent);
      expect(unified!.track, accent);
      expect(unified!.playlist, accent);
      expect(unified!.genre, accent);
      expect(unified!.atDepth(unified!.artist, 0), accent);
      expect(unified!.atDepth(unified!.artist, 1).a, closeTo(0.78, 0.01));
      expect(unified!.atDepth(unified!.album, 2).a, closeTo(0.60, 0.01));
    },
  );
}
