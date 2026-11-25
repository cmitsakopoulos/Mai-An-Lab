import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:fake_async/fake_async.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/adapters.dart';
import 'package:mai_an_lab/assistant/metadata_tools.dart';
import 'package:mai_an_lab/assistant/runner.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/metadata/metadata_service.dart';
import 'package:mai_an_lab/metadata/workbench_controller.dart';
import 'package:mai_an_lab/metadata/workbench_view.dart';
import 'package:mai_an_lab/ui/tokens.dart';
import 'package:mai_an_lab/player/queue_store.dart';

import 'phase7a_test.dart' as p7a;
import 'sync_fakes.dart' show RecordingBridge;

/// Phase 7b: Jarvis's metadata write tools against Flet's own outputs
/// (`tool/export_phase7a_goldens.py`, "tools"), and the workbench's
/// plumbing: the automatic pass's scheduling, the coalesced walk refresh,
/// the controller on the 7a fixture, and the Library's entry points.

Object? _j(Object? v) => jsonDecode(jsonEncode(v));

class _NoPlayer implements AssistantPlayer {
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  late Directory tmp;
  final savedBridge = NotificationBridge.instance;

  setUp(() {
    tmp = Directory.systemTemp.createTempSync('mai_an_7b_');
    NotificationBridge.instance = RecordingBridge();
  });
  tearDown(() {
    NotificationBridge.instance = savedBridge;
    tmp.deleteSync(recursive: true);
  });

  group('metadata write tools', () {
    final cases = (p7a.g['tools']! as List<Object?>)
        .cast<Map<String, Object?>>();
    for (final (i, c) in cases.indexed) {
      test('${c['tool']} #$i ${jsonEncode(c['args'])}', () async {
        final repo = await p7a.fixtureRepo(tmp, 'tool_$i');
        addTearDown(repo.close);
        var refreshes = 0;
        final handler = buildMetadataWriteHandler(
          repository: () => repo,
          researcher: p7a.goldenResearcher,
          refreshWalkModels: () async => refreshes++,
        );
        final runner = AssistantRunner(
          library: RepoAssistantLibrary(repo),
          player: _NoPlayer(),
        );
        runner.agentReset();
        final res = await handler(
          c['tool']! as String,
          Map<String, Object?>.of(c['args']! as Map<String, Object?>),
          runner,
        );
        expect(_j(res), c['result']);
        final ir = runner.agentInterrupt;
        if (c['interrupt'] == null) {
          expect(ir, isNull);
          return;
        }
        expect({
          'spoken': ir!.spoken,
          'displayed': ir.displayed,
        }, c['interrupt']);
        final done = await runner.pending!.onYes!();
        expect({
          'spoken': done.spoken,
          'displayed': done.displayed,
        }, c['after']);
        expect(refreshes, c['refreshes']);
        p7a.expectState(await p7a.dump(repo), c['state']);
      });
    }

    test('no writable library', () async {
      final handler = buildMetadataWriteHandler(
        repository: () => null,
        researcher: p7a.goldenResearcher,
        refreshWalkModels: () async => null,
      );
      final runner = AssistantRunner(
        library: _NoLibrary(),
        player: _NoPlayer(),
      );
      expect(await handler('apply_artist_metadata', {}, runner), {
        'success': false,
        'error': 'Library unavailable.',
      });
    });
  });

  group('MetadataService', () {
    MetadataService service(
      LibraryRepository repo, {
      bool auto = true,
      List<String>? reloads,
    }) => MetadataService(
      repository: () => repo,
      reloadLibrary: () async => reloads?.add('reload'),
      autoEnrichEnabled: () => auto,
      preferredProvider: () => 'musicbrainz',
      refreshDebounce: const Duration(milliseconds: 20),
    );

    test('auto-enrich off: the pass never runs', () async {
      final repo = await p7a.fixtureRepo(tmp, 'off');
      addTearDown(repo.close);
      final before = await p7a.dump(repo);
      await service(repo, auto: false).runPending();
      expect(_j(await p7a.dump(repo)), _j(before));
      expect((NotificationBridge.instance as RecordingBridge).calls, isEmpty);
    });

    test('refresh_walk_models: a burst is one rebuild', () async {
      final repo = await p7a.fixtureRepo(tmp, 'refresh');
      addTearDown(repo.close);
      final reloads = <String>[];
      final s = service(repo, reloads: reloads);
      addTearDown(s.dispose);
      final outs = await Future.wait([
        s.refreshWalkModels(),
        s.refreshWalkModels(),
        s.refreshWalkModels(),
      ]);
      expect(reloads, ['reload']);
      expect(
        outs.map((o) => o.keys.toSet()),
        everyElement({'genre_fix', 'model_pairs'}),
      );
      expect(identical(outs[0], outs[2]), isTrue);
    });

    test('whenIdle: completes once the running pass ends', () async {
      var reads = 0;
      final s = MetadataService(
        repository: () {
          reads++;
          return null;
        },
        reloadLibrary: () async {},
        autoEnrichEnabled: () => true,
        preferredProvider: () => 'musicbrainz',
      );
      await s.whenIdle();
      // A pass with no library still runs (and ends) as one pass.
      final run = s.runPending();
      expect(s.running, isTrue);
      var idle = false;
      unawaited(s.whenIdle().then((_) => idle = true));
      await run;
      await pumpEventQueue();
      expect(idle, isTrue);
      expect(s.running, isFalse);
      expect(reads, greaterThan(0));
    });

    test('schedule: coalesced, and one boot pass per process', () {
      fakeAsync((async) {
        var passes = 0;
        final s = MetadataService(
          repository: () => null,
          reloadLibrary: () async {},
          autoEnrichEnabled: () {
            passes++;
            return false;
          },
          preferredProvider: () => 'musicbrainz',
        );
        s
          ..scheduleBoot()
          ..scheduleBoot()
          ..schedule()
          ..schedule();
        async.elapse(const Duration(seconds: 29));
        expect(passes, 0);
        async.elapse(const Duration(seconds: 1));
        expect(passes, 1);
        // The boot pass is spent; a scan's schedule runs 5 s later.
        s.scheduleBoot();
        async.elapse(const Duration(seconds: 30));
        expect(passes, 1);
        s.schedule();
        async.elapse(const Duration(seconds: 5));
        expect(passes, 2);
        s.dispose();
      });
    });
  });

  group('WorkbenchController', () {
    late LibraryRepository repo;
    late WorkbenchController wb;
    late MetadataService svc;
    late Prefs prefs;
    final snacks = <String>[];

    setUp(() async {
      repo = await p7a.fixtureRepo(tmp, 'wb');
      prefs = Prefs(tmp.path);
      await prefs.load();
      svc = MetadataService(
        repository: () => repo,
        reloadLibrary: () async {},
        autoEnrichEnabled: () => true,
        preferredProvider: () => 'musicbrainz',
        refreshDebounce: const Duration(milliseconds: 10),
      );
      wb = WorkbenchController(
        repository: () => repo,
        service: svc,
        researcher: p7a.goldenResearcher,
        prefs: prefs,
        musicBrainz: p7a.mbClient,
      );
      snacks.clear();
      wb.snacks.listen((s) => snacks.add(s.$1));
    });
    tearDown(() async {
      wb.dispose();
      svc.dispose();
      await repo.close();
    });

    test('reload: everyone in confidence order, suggestions waiting', () async {
      await wb.reload();
      expect(wb.loaded, isTrue);
      expect(wb.everyone, isNotEmpty);
      // Least sure first; "most" flips it.
      final least = [for (final g in wb.visible()) wb.sureTier(g)];
      expect(least, [...least]..sort());
      wb.setSort('most');
      final most = [for (final g in wb.visible()) wb.sureTier(g)];
      expect(most, [...most]..sort((a, b) => b.compareTo(a)));
      // The fixture's pending AI answers are suggestions (Drifter's
      // country-only answer included).
      final names = {for (final g in wb.suggestionRows()) g['artist_name']};
      expect(names, containsAll(['Nomad', 'Drifter']));
      wb.reviewSuggestions();
      expect(wb.selected, names);
      expect(wb.search, '');
    });

    test('search matches names and genres', () async {
      await wb.filterGenre('alexiou');
      expect({
        for (final g in wb.visible()) g['artist_name'],
      }, containsAll(['Haris Alexiou']));
      wb.setSearch('zzz-nothing');
      expect(wb.visible(), isEmpty);
    });

    test(
      'focusArtist: the editor opens, even before the page listens',
      () async {
        await wb.focusArtist('Moose');
        final waiting = wb.takeWaitingEditor();
        expect(waiting?['artist_name'], 'Moose');
        expect(wb.takeWaitingEditor(), isNull);
        final opened = <Object?>[];
        final sub = wb.openEditor.listen((r) => opened.add(r['artist_name']));
        await wb.focusArtist('Kasabian');
        await pumpEventQueue();
        await sub.cancel();
        expect(opened, ['Kasabian']);
        expect(wb.takeWaitingEditor(), isNull);
      },
    );

    test('concurrent reloads share one pass', () async {
      final a = wb.reload();
      final b = wb.reload();
      expect(identical(a, b), isTrue);
      await a;
    });

    test('accept, save genres, recent genres', () async {
      await wb.reload();
      await wb.accept(['Nomad']);
      await pumpEventQueue();
      expect(snacks, contains('Accepted 1 suggestion'));
      expect(wb.aiPending.containsKey('Nomad'), isFalse);
      await wb.saveGenres(['Kasabian', 'Slipknot'], ['rock'], replace: true);
      await pumpEventQueue();
      expect(snacks, contains('2 artists → rock'));
      expect(wb.recentGenres(), ['rock']);
      wb.rememberGenres(['indie', 'rock']);
      expect(wb.recentGenres(), ['indie', 'rock']);
      await Future<void>.delayed(const Duration(milliseconds: 200));
      expect(snacks, contains('Auto-Play updated.'));
      final rows = await repo.debugSelect(
        "SELECT artist_name, genres, source FROM artist_enrichment "
        "WHERE artist_name IN ('Kasabian', 'Slipknot') ORDER BY artist_name",
      );
      for (final r in rows) {
        expect(
          [
            for (final g in jsonDecode(r[1]! as String) as List)
              (g as Map)['name'],
          ],
          ['rock'],
        );
      }
    });
  });

  testWidgets('WorkbenchPage lays out on a phone, focused editor opens', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(360 * 3, 760 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final repo = (await tester.runAsync(() => p7a.fixtureRepo(tmp, 'page')))!;
    final prefs = Prefs(tmp.path);
    final svc = MetadataService(
      repository: () => repo,
      reloadLibrary: () async {},
      autoEnrichEnabled: () => false,
      preferredProvider: () => 'musicbrainz',
    );
    final wb = WorkbenchController(
      repository: () => repo,
      service: svc,
      researcher: p7a.goldenResearcher,
      prefs: prefs,
      musicBrainz: p7a.mbClient,
    );
    // The Library's edit button: focus first, then the page opens.
    await tester.runAsync(() => wb.focusArtist('Moose'));
    await tester.pumpWidget(
      MaterialApp(
        theme: Tokens.theme(),
        home: Scaffold(body: WorkbenchPage(controller: wb)),
      ),
    );
    for (var i = 0; i < 6; i++) {
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 30)),
      );
      await tester.pump(const Duration(milliseconds: 300));
    }
    expect(tester.takeException(), isNull);
    expect(find.text('Sync · MusicBrainz'), findsOneWidget);
    expect(find.textContaining('Research '), findsWidgets);
    expect(find.text('Moose'), findsWidgets);
    await tester.pumpWidget(const SizedBox());
    wb.dispose();
    svc.dispose();
    await tester.runAsync(repo.close);
  });
}

class _NoLibrary implements AssistantLibrary {
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
