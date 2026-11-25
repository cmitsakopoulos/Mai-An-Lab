// Phase 6d: the on-phone DSP compute around the extractor: persistence
// (update_track_features), bulk_analyze_library, the three Flet entry
// flows (Library, Settings, Recompute All), and the dataSync timeout.
import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:maian_native/maian_native.dart';
import 'package:mai_an_lab/analysis/audio_features.dart';
import 'package:mai_an_lab/analysis/dsp_analyser.dart';
import 'package:mai_an_lab/analysis/library_analysis.dart';
import 'package:mai_an_lab/core/progress_notify.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:sqlite3/sqlite3.dart';

AudioFeatures _features(double seed) => AudioFeatures(
  bpm: 100 + seed,
  energy: .5,
  brightness: .2,
  rolloff: .3,
  beatStrength: .4,
  spectralFlatness: .1,
  spectralContrast: .25,
  keyIndex: seed.toInt() % 24,
  mfccMean: Float32List.fromList(List.generate(20, (i) => seed + i)),
  mfccStd: Float32List.fromList(List.generate(20, (i) => 1.0 + i)),
  chroma: Float32List.fromList(List.generate(12, (i) => (i + seed) / 100)),
  rhythm: Float32List.fromList(List.generate(16, (i) => i / 16)),
);

class _Decoder implements PcmDecoder {
  @override
  Future<DecodedPcm> decode(String path) async =>
      (path: path, sampleRate: kDspSampleRate);
}

class _Bridge implements NotificationBridge {
  final calls = <Map<String, Object?>>[];
  final _actions = StreamController<NotificationAction>.broadcast();
  void emit(String job, String action) =>
      _actions.add((job: job, action: action));

  @override
  Stream<NotificationAction> get actions => _actions.stream;
  @override
  Future<void> showProgress({
    required String job,
    required String title,
    required String content,
    required int progress,
    required int total,
    required bool done,
    required bool cancellable,
    String? actionLabel,
    String? actionTarget,
  }) async => calls.add({
    'job': job,
    'title': title,
    'content': content,
    'done': done,
    'cancellable': cancellable,
  });
  @override
  Future<void> showAlert({
    required String key,
    required String title,
    required String content,
    String navigateTo = '',
    String? actionLabel,
    String? actionTarget,
    bool urgent = false,
    String icon = '',
  }) async => calls.add({'alert': key, 'action': actionTarget});
  @override
  Future<void> cancelAlert(String key) async => calls.add({'cancel': key});
}

class _Host implements AnalysisHost {
  _Host(this.repository);
  @override
  final LibraryRepository? repository;
  int rebuilds = 0;
  Object? failRebuild;
  @override
  Future<AcousticBuildResult> rebuildSimilarityGraph({
    bool metadata = true,
  }) async {
    rebuilds++;
    if (failRebuild case final e?) throw e;
    return (tracks: 0, dims: 0, redundant: <String>{});
  }
}

void main() {
  late Directory dir;
  late String dbPath;
  late LibraryRepository repo;

  setUp(() async {
    dir = Directory.systemTemp.createTempSync('mai_an_dsp');
    dbPath = '${dir.path}/library.db';
    repo = await LibraryRepository.open(dbPath, writable: true);
    final db = sqlite3.open(dbPath);
    db.execute('''
      INSERT INTO artists (id, name) VALUES (1, 'Ana');
      INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'First');
      INSERT INTO tracks (album_id, title, path, added_date) VALUES
        (1, 'A', '/m/a.flac', 1), (1, 'B', '/m/b.flac', 2),
        (1, 'C', '/m/c.flac', 3);
      INSERT INTO play_counts (track_path, count) VALUES ('/m/b.flac', 7);
    ''');
    db.close();
  });

  tearDown(() async {
    await repo.close();
    dir.deleteSync(recursive: true);
  });

  test(
    'update_track_features: upserts the v5 row, keeps play counts',
    () async {
      expect(await repo.tracksMissingFeatures(), hasLength(3));
      await repo.updateTrackFeatures('/m/a.flac', _features(1));
      await repo.updateTrackFeatures('/m/b.flac', _features(2));
      expect(await repo.tracksMissingFeatures(), ['/m/c.flac']);
      final db = sqlite3.open(dbPath);
      final r = db
          .select(
            'SELECT count, bpm, key_index, timbre, features_version, rolloff '
            "FROM play_counts WHERE track_path = '/m/b.flac'",
          )
          .first;
      db.close();
      expect(r['count'], 7);
      expect(r['bpm'], 102.0);
      expect(r['key_index'], 2);
      expect(r['rolloff'], closeTo(.3, 1e-12));
      expect((r['timbre'] as Uint8List).length, kEmbedDims * 4);
      expect(r['features_version'], kFeaturesVersion);
    },
  );

  group('bulk_analyze_library', () {
    test('counts failures, reports every track, persists the rest', () async {
      final seen = <(int, int, String, int)>[];
      final stored = <String>[];
      final res = await bulkAnalyzeLibrary(
        missing: ['/a', '/bad', '/c', '/persist-fails'],
        decoder: _Decoder(),
        analyse: (_, p) async {
          if (p == '/bad') throw StateError('decode');
          return _features(1);
        },
        persist: (p, f) async {
          if (p == '/persist-fails') throw StateError('db');
          stored.add(p);
        },
        progress: (d, t, c, f) => seen.add((d, t, c, f)),
      );
      expect(res, (analysed: 2, failed: 2, total: 4));
      expect(stored, ['/a', '/c']);
      expect(seen, [
        (1, 4, '/a', 0),
        (2, 4, '/bad', 1),
        (3, 4, '/c', 1),
        (4, 4, '/persist-fails', 2),
      ]);
    });

    test('cancel is polled before each track', () async {
      var n = 0;
      final res = await bulkAnalyzeLibrary(
        missing: ['/a', '/b', '/c'],
        decoder: _Decoder(),
        analyse: (_, _) async => _features(1),
        persist: (_, _) async => n++,
        cancelled: () => n >= 2,
      );
      expect(res, (analysed: 2, failed: 0, total: 3));
    });

    test('nothing missing: nothing runs', () async {
      final res = await bulkAnalyzeLibrary(
        missing: const [],
        decoder: _Decoder(),
        persist: (_, _) async => fail('persisted'),
      );
      expect(res, (analysed: 0, failed: 0, total: 0));
    });
  });

  group('DspAnalysis', () {
    late _Bridge bridge;
    late _Host host;
    setUp(() {
      bridge = _Bridge();
      host = _Host(repo);
    });

    DspAnalysis analysis({
      PcmDecoder? decoder,
      Future<AudioFeatures> Function(PcmDecoder, String)? analyse,
    }) => DspAnalysis(
      host,
      decoder: () async => decoder,
      bridge: bridge,
      analyse: analyse ?? (_, p) async => _features(p.length.toDouble()),
    );

    test('Library flow: card status, notification, graph rebuild', () async {
      final a = analysis(decoder: _Decoder());
      final statuses = <String>[];
      a.addListener(() => statuses.add(a.status));
      final notices = <DspNotice>[];
      final res = await a.run(DspEntry.library, notice: notices.add);
      expect(res, (analysed: 3, failed: 0, total: 3));
      expect(await repo.tracksMissingFeatures(), isEmpty);
      expect(host.rebuilds, 1);
      expect(notices, isEmpty);
      expect(statuses.first, 'Analysing 1 / 3 tracks...');
      expect(statuses, contains('Linking similar tracks...'));
      expect(statuses.last, '');
      expect(a.running, isFalse);
      expect(bridge.calls.first, {'cancel': 'scan'});
      final progress = bridge.calls.where((c) => c['job'] == 'dsp');
      expect(progress.first['title'], 'Analysing library');
      expect(progress.first['cancellable'], isTrue);
      expect(progress.last, containsPair('done', true));
      expect(
        progress.last['content'],
        'Analysed 3 tracks. Similarity walks are ready.',
      );
    });

    test(
      'Library flow, nothing missing: rebuild only, no notification',
      () async {
        for (final p in ['/m/a.flac', '/m/b.flac', '/m/c.flac']) {
          await repo.updateTrackFeatures(p, _features(1));
        }
        final notices = <DspNotice>[];
        await analysis(
          decoder: _Decoder(),
        ).run(DspEntry.library, notice: notices.add);
        expect(notices.single.text, 'All tracks already analysed.');
        expect(host.rebuilds, 1);
        expect(bridge.calls.where((c) => c['job'] == 'dsp'), isEmpty);
      },
    );

    test(
      'Settings flows: their snackbars; Recompute All purges first',
      () async {
        await repo.updateTrackFeatures('/m/a.flac', _features(1));
        final notices = <String>[];
        await analysis(
          decoder: _Decoder(),
        ).run(DspEntry.settings, notice: (n) => notices.add(n.text));
        expect(notices, [
          'Analysing 2 tracks — this can take a while.',
          'DSP features, edges, and PCA space rebuilt.',
        ]);
        notices.clear();
        await analysis(
          decoder: _Decoder(),
        ).run(DspEntry.recomputeAll, notice: (n) => notices.add(n.text));
        expect(notices, [
          'Purging acoustic features and starting full DSP re-analysis...',
          'Recomputing features for 3 tracks — this can take a while.',
          'DSP features, edges, and PCA space recomputed.',
        ]);
        expect(
          bridge.calls.where((c) => c['done'] == true).last['content'],
          'Analysis complete. Similarity walks are ready.',
        );
      },
    );

    test('no decoder: the Flet error, nothing written', () async {
      final notices = <DspNotice>[];
      final a = analysis();
      await a.run(DspEntry.settings, notice: notices.add);
      expect(notices.single.kind, DspNoticeKind.error);
      expect(
        notices.single.text,
        'Audio service not ready — native analyser unavailable.',
      );
      expect(a.running, isFalse);
      expect(await repo.tracksMissingFeatures(), hasLength(3));
    });

    test('one run at a time; Cancel stops between tracks', () async {
      final gate = Completer<void>();
      late DspAnalysis a;
      a = analysis(
        decoder: _Decoder(),
        analyse: (_, p) async {
          await gate.future;
          return _features(1);
        },
      );
      final first = a.run(DspEntry.library);
      await pumpEventQueue();
      expect(a.running, isTrue);
      final notices = <String>[];
      await a.run(DspEntry.settings, notice: (n) => notices.add(n.text));
      expect(notices, ['Analysis is already running.']);
      a.cancel();
      gate.complete();
      final res = await first;
      expect(res!.analysed, 1);
      expect(host.rebuilds, 0);
      expect(
        bridge.calls.where((c) => c['done'] == true).last['content'],
        'Analysis cancelled.',
      );
    });

    test('dataSync timeout winds the run down with its own summary', () async {
      final gate = Completer<void>();
      final a = analysis(
        decoder: _Decoder(),
        analyse: (_, p) async {
          await gate.future;
          return _features(1);
        },
      );
      final run = a.run(DspEntry.library);
      await pumpEventQueue();
      bridge.emit('all', 'fgs_timeout');
      await pumpEventQueue();
      gate.complete();
      await run;
      expect(host.rebuilds, 0);
      expect(
        bridge.calls.where((c) => c['done'] == true).last['content'],
        startsWith('Analysis paused'),
      );
    });

    test('graph failure: Library summary says so', () async {
      host.failRebuild = StateError('svd');
      final notices = <String>[];
      await analysis(
        decoder: _Decoder(),
      ).run(DspEntry.library, notice: (n) => notices.add(n.text));
      expect(notices.single, startsWith('Graph rebuild failed'));
      expect(
        bridge.calls.where((c) => c['done'] == true).last['content'],
        'Analysed 3 tracks; graph rebuild failed.',
      );
    });
  });

  test('dspProgressText: dsp_progress_cb', () {
    expect(dspProgressText(0, 10, 0, Duration.zero), '0 / 10 tracks');
    expect(
      dspProgressText(2, 10, 1, const Duration(seconds: 6)),
      '2 / 10 tracks · 1 failed · ~24s left',
    );
    expect(
      dspProgressText(10, 10, 0, const Duration(minutes: 9)),
      '10 / 10 tracks',
    );
  });
}
