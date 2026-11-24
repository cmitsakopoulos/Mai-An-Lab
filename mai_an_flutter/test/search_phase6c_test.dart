// Phase 6c: Search UI, hero/album/artist cards, pagination, source switching,
// missing-credentials banner, multi-select, Quality selector sheet, download
// history, AppDownloadHost source downloads, stream previews, Library
// Redownload, and Jarvis search_online hook.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/app_shell.dart';
import 'package:mai_an_lab/assistant/online_search.dart';
import 'package:mai_an_lab/assistant/runner.dart' hide Row;
import 'package:mai_an_lab/assistant/tool_executor.dart';
import 'package:mai_an_lab/core/config_file.dart';
import 'package:mai_an_lab/core/haptics.dart';
import 'package:mai_an_lab/data/library_repository.dart';
import 'package:mai_an_lab/downloads/app_download_host.dart';
import 'package:mai_an_lab/downloads/companion_download.dart'
    show DownloadProgress;
import 'package:mai_an_lab/downloads/download_history.dart';
import 'package:mai_an_lab/downloads/download_queue.dart';
import 'package:mai_an_lab/downloads/quality_selector.dart';
import 'package:mai_an_lab/library/browse_controller.dart';
import 'package:mai_an_lab/library/library_controller.dart';
import 'package:mai_an_lab/player/play_queue.dart';
import 'package:mai_an_lab/player/playback_backend.dart';
import 'package:mai_an_lab/player/player_controller.dart';
import 'package:mai_an_lab/player/queue_entry.dart';
import 'package:mai_an_lab/player/queue_store.dart';
import 'package:mai_an_lab/search/landing_controller.dart';
import 'package:mai_an_lab/search/local_search.dart';
import 'package:mai_an_lab/search/search_controller.dart';
import 'package:mai_an_lab/search/search_screen.dart';
import 'package:mai_an_lab/settings/config_controller.dart';
import 'package:mai_an_lab/settings/settings_search.dart';
import 'package:mai_an_lab/sources/downloadable.dart';
import 'package:mai_an_lab/sources/source_client.dart';
import 'package:mai_an_lab/sources/source_config.dart';
import 'package:mai_an_lab/sources/streamrip_search.dart';
import 'package:mai_an_lab/ui/tokens.dart';

const _setup = [
  "INSERT INTO artists (id, name) VALUES (1, 'Burial'), (2, 'Boards of Canada')",
  "INSERT INTO albums (id, artist_id, title, year) VALUES "
      "(1, 1, 'Untrue', '2007'), (2, 2, 'Geogaddi', '2002')",
  "INSERT INTO tracks (album_id, title, track_num, path, added_date, duration) VALUES "
      "(1, 'Archangel', 1, '/music/1.flac', 1, 200), "
      "(1, 'Near Dark', 2, '/music/2.flac', 2, 200), "
      "(2, 'Music Is Math', 1, '/music/3.flac', 3, 200)",
];

class _StubDownloadable extends Downloadable {
  _StubDownloadable({
    required super.url,
    required super.extension,
    required this.payload,
  }) : super(http: _NoopHttp(), initialSize: payload.length);

  final List<int> payload;

  @override
  Future<void> download(
    String path,
    BytesProgressCallback callback, {
    bool Function()? isCancelled,
  }) async {
    if (isCancelled != null && isCancelled()) return;
    await File(path).writeAsBytes(payload);
    callback(payload.length);
  }
}

class _NoopHttp implements SourceHttpClient {
  @override
  Future<void> close() async {}
  @override
  Future<SourceStreamResponse> openStream(
    Uri uri, {
    Map<String, String>? headers,
    bool followRedirects = true,
    Duration? timeout,
  }) => throw UnimplementedError();
  @override
  Future<SourceHttpResponse> request(
    String method,
    Uri uri, {
    Map<String, String>? headers,
    List<int>? body,
    bool followRedirects = true,
    Duration? timeout,
  }) => throw UnimplementedError();
}

class _FakeSourceClient implements SourceClient {
  _FakeSourceClient({
    required this.source,
    this.searchHandler,
    this.metadataHandler,
  });

  @override
  final String source;
  @override
  int get maxQuality => source == 'deezer' ? 2 : 4;
  @override
  bool loggedIn = false;
  bool _closed = false;
  int searchCalls = 0;

  final Future<List<Map<String, Object?>>> Function(String mtype, String query)?
  searchHandler;
  final Future<Map<String, Object?>> Function(
    String id,
    String mtype,
    int? limit,
    int? offset,
  )?
  metadataHandler;

  @override
  bool get isClosed => _closed;

  @override
  Future<void> login() async => loggedIn = true;

  @override
  Future<List<Map<String, Object?>>> search(
    String mediaType,
    String query, {
    int limit = 500,
    int offset = 0,
    ProgressStageCallback? progressCallback,
  }) async {
    searchCalls++;
    progressCallback?.call('HTTP Request', 'Querying $mediaType…');
    if (searchHandler != null) return searchHandler!(mediaType, query);
    return const [];
  }

  @override
  Future<Map<String, Object?>> getMetadata(
    String itemId,
    String mediaType, {
    int? limit,
    int? offset,
  }) async {
    if (metadataHandler != null) {
      return metadataHandler!(itemId, mediaType, limit, offset);
    }
    return const {};
  }

  @override
  Future<Downloadable> getDownloadable(String itemId, int quality) async {
    return _StubDownloadable(
      url: 'https://cdn.example/$source/$itemId.mp3',
      extension: 'mp3',
      payload: List<int>.filled(417, 0xFF),
    );
  }

  @override
  Future<List<int>?> fetchBytes(String url) async => null;

  @override
  Future<void> close() async => _closed = true;
}

class _FakePlaybackBackend implements PlaybackBackend {
  final _changes = StreamController<void>.broadcast();
  final _failures = StreamController<PlaybackFailure>.broadcast();
  final _listens = StreamController<Listen>.broadcast();

  List<QueueEntry> rows = [];
  int? idx;
  bool _playing = false;
  Processing _processing = Processing.idle;
  bool failNextLoad = false;

  @override
  Stream<void> get changes => _changes.stream;
  @override
  Stream<PlaybackFailure> get failures => _failures.stream;
  @override
  Stream<Listen> get listens => _listens.stream;
  @override
  bool get playing => _playing;
  @override
  Processing get processing => _processing;
  @override
  int? get currentIndex => idx;
  @override
  int get length => rows.length;
  @override
  Duration get position => Duration.zero;
  @override
  Duration? get duration => const Duration(seconds: 30);
  @override
  List<int> get shuffleIndices => const [];
  @override
  String? idAt(int? i) =>
      (i != null && i >= 0 && i < rows.length) ? rows[i].src : null;
  @override
  String? artFor(String src) => null;
  @override
  void setPaths({required String ledgerPath, required String resumePath}) {}

  @override
  Future<void> setPlaylist(
    List<QueueEntry> entries,
    int startIndex, {
    required bool autoplay,
    bool? shuffle,
    bool keepListen = false,
  }) async {
    rows = List.of(entries);
    idx = startIndex;
    if (failNextLoad) {
      failNextLoad = false;
      _playing = false;
      _processing = Processing.idle;
      _failures.add((
        message: 'error',
        title: entries.isNotEmpty ? entries.first.title : '',
        artist: '',
        action: 'stopped',
      ));
      _changes.add(null);
      return;
    }
    _playing = autoplay;
    _processing = Processing.ready;
    _changes.add(null);
  }

  @override
  Future<void> play() async {
    _playing = true;
    _changes.add(null);
  }

  @override
  Future<void> pause() async {
    _playing = false;
    _changes.add(null);
  }

  @override
  Future<void> stop() async {
    _playing = false;
    _processing = Processing.idle;
    _changes.add(null);
  }

  @override
  Future<void> seek(Duration position) async {}
  @override
  Future<void> skipTo(int index, {bool autoplay = true}) async => idx = index;
  @override
  Future<void> skipToNext() async {}
  @override
  Future<void> skipToPrevious() async {}
  @override
  Future<void> setRepeat(Repeat mode) async {}
  @override
  Future<void> setShuffle(bool enabled) async {}
  @override
  Future<void> splice(List<SpliceEdit> edits) async {}
  @override
  Future<void> move(int from, int to) async {}
  @override
  Future<void> setEqBandGain(int index, double gainDb) async {}
  @override
  Future<void> setLoudnessBoost(double boostDb) async {}
  @override
  Future<EqualizerInfo?> equalizerInfo() async => null;
}

class _FakeDownloadHost implements DownloadHost {
  final snacks = <String>[];
  final downloaded = <DownloadJob>[];

  @override
  void snack(String text) => snacks.add(text);
  @override
  Future<String> downloadDir() async => Directory.systemTemp.path;
  @override
  Future<List<String>> download(
    DownloadJob job,
    String dir, {
    required DownloadProgress progress,
    required bool Function() isCancelled,
  }) async {
    downloaded.add(job);
    return const [];
  }

  @override
  Future<void> Function(String path, Map<String, Object?> dsp)?
  get persistFeatures => null;
  @override
  void downloadSucceeded() {}
  @override
  void quietScan() {}
  @override
  void transferFinished(String transferId, {required bool ok}) {}
}

class _DummyAssistantLibrary implements AssistantLibrary {
  @override
  dynamic noSuchMethod(Invocation invocation) => Future.value(const <Object?>[]);
}

class _DummyAssistantPlayer implements AssistantPlayer {
  @override
  List<Map<String, Object?>> get queue => const [];
  @override
  int get currentIndex => 0;
  @override
  String get currentPath => '';
  @override
  String get currentTrack => '';
  @override
  String get currentArtist => '';
  @override
  String get currentAlbum => '';
  @override
  double get duration => 0;
  @override
  bool get isPlaying => false;
  @override
  bool isShuffle = false;
  @override
  dynamic noSuchMethod(Invocation invocation) {}
}

Map<String, Object?> _qobuzTrack(String id, String title, String artist) => {
  'id': id,
  'title': title,
  'performer': {'name': artist},
  'album': {
    'id': 'alb_$id',
    'title': 'Untrue',
    'release_date_original': '2007-11-05',
    'image': {'small': ''},
  },
};

Map<String, Object?> _qobuzAlbum(
  String id,
  String title,
  String artist, {
  int tracksCount = 10,
}) => {
  'id': id,
  'title': title,
  'artist': {'name': artist},
  'release_date_original': '2007-11-05',
  'tracks_count': tracksCount,
  'image': {'small': ''},
};

Map<String, Object?> _qobuzArtist(String id, String name, int albumsCount) => {
  'id': id,
  'name': name,
  'albums_count': albumsCount,
};

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('Phase 6c Python goldens', () {
    late Map<String, dynamic> golden;

    setUpAll(() {
      golden =
          jsonDecode(File('test/goldens/phase6c.json').readAsStringSync())
              as Map<String, dynamic>;
    });

    test('hiresSubtitle matches Python quality_selector.py', () {
      for (final raw in golden['hires_subtitle'] as List<dynamic>) {
        final c = raw as Map<String, dynamic>;
        final records = [
          for (final r in c['records'] as List<dynamic>)
            Map<String, Object?>.from(r as Map),
        ];
        expect(hiresSubtitle(records), c['expected'], reason: '${c['name']}');
      }
    });

    test('pickHeroResult matches Python search.py', () {
      for (final raw in golden['hero_result'] as List<dynamic>) {
        final c = raw as Map<String, dynamic>;
        final picked = pickHeroResult(
          '${c['query']}',
          tracks: [
            for (final r in c['tracks'] as List<dynamic>)
              Map<String, Object?>.from(r as Map),
          ],
          albums: [
            for (final r in c['albums'] as List<dynamic>)
              Map<String, Object?>.from(r as Map),
          ],
          artists: [
            for (final r in c['artists'] as List<dynamic>)
              Map<String, Object?>.from(r as Map),
          ],
        );
        expect(picked?['id'], c['expected_id'], reason: '${c['name']}');
      }
    });
  });

  group('DownloadHistoryStore', () {
    late Directory dir;

    setUp(() => dir = Directory.systemTemp.createTempSync('mai_an_dlhist'));
    tearDown(() => dir.deleteSync(recursive: true));

    test('deduplicates by url, caps at 100, writes 4-space JSON', () async {
      final store = DownloadHistoryStore(
        dir.path,
        clock: () => DateTime(2026, 11, 24, 15, 30),
      );
      for (var i = 1; i <= 105; i++) {
        await store.add({
          'name': 'Track $i',
          'artist': 'Artist',
          'source': 'qobuz',
          'url': 'https://www.qobuz.com/track/$i',
        });
      }
      var list = await store.load();
      expect(list.length, 100);
      expect(list.first['name'], 'Track 105');
      expect(list.first['date'], '2026-11-24 15:30');

      // Re-adding existing URL moves it to front without duplicating.
      await store.add({
        'name': 'Track 50',
        'artist': 'Artist',
        'source': 'qobuz',
        'url': 'https://www.qobuz.com/track/50',
      });
      list = await store.load();
      expect(list.length, 100);
      expect(list.first['url'], 'https://www.qobuz.com/track/50');
      expect(
        list.where((e) => e['url'] == 'https://www.qobuz.com/track/50').length,
        1,
      );
      expect(store.file.readAsStringSync(), contains('    {'));
    });
  });

  group('QualitySelector sheet', () {
    testWidgets('shows Deezer-aware Hi-Res subtitle and enqueues picked tier', (
      tester,
    ) async {
      final host = _FakeDownloadHost();
      final queue = DownloadQueue(host)..holdWorker = true;
      final snacks = <String>[];

      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: Scaffold(
            body: Builder(
              builder:
                  (ctx) => ElevatedButton(
                    onPressed: () {
                      showQualitySelector(
                        ctx,
                        record: {
                          'id': '123',
                          'source': 'deezer',
                          'media_type': 'track',
                          'ui_title': 'Archangel',
                        },
                        queue: queue,
                        onSnack: (m, {icon}) => snacks.add(m),
                      );
                    },
                    child: const Text('Pick'),
                  ),
            ),
          ),
        ),
      );

      await tester.tap(find.text('Pick'));
      await tester.pumpAndSettle();

      expect(find.text('Select Quality'), findsOneWidget);
      expect(find.text('Archangel'), findsOneWidget);
      expect(find.text("16-bit FLAC (Deezer's best)"), findsOneWidget);

      await tester.tap(find.text('Hi-Res'));
      await tester.pumpAndSettle();

      expect(queue.pending.length, 1);
      expect(queue.pending.first.metadata['quality'], 2); // Deezer max = 2
      expect(queue.pending.first.qualityLabel, 'HIRES');
      expect(snacks, ['Added “Archangel” to downloads (HIRES).']);
    });

    testWidgets('skips sheet when defaultQuality is configured in Settings', (
      tester,
    ) async {
      final host = _FakeDownloadHost();
      final queue = DownloadQueue(host)..holdWorker = true;
      final snacks = <String>[];
      var doneCalled = false;

      await tester.pumpWidget(
        MaterialApp(
          theme: Tokens.theme(),
          home: Scaffold(
            body: Builder(
              builder:
                  (ctx) => ElevatedButton(
                    onPressed: () {
                      showQualitySelectorBatch(
                        ctx,
                        records: [
                          {'id': '1', 'source': 'qobuz', 'ui_title': 'A'},
                          {'id': '2', 'source': 'qobuz', 'ui_title': 'B'},
                        ],
                        queue: queue,
                        defaultQuality: 'cd',
                        onSnack: (m, {icon}) => snacks.add(m),
                        onDone: () => doneCalled = true,
                      );
                    },
                    child: const Text('Batch'),
                  ),
            ),
          ),
        ),
      );

      await tester.tap(find.text('Batch'));
      await tester.pumpAndSettle();

      expect(find.text('Select Quality'), findsNothing);
      expect(queue.pending.length, 2);
      expect(queue.pending.first.metadata['quality'], 2);
      expect(doneCalled, isTrue);
      expect(snacks, ['Added 2 items to downloads (CD).']);
    });
  });

  group('AppDownloadHost source workflow', () {
    late Directory dir;

    setUp(() => dir = Directory.systemTemp.createTempSync('mai_an_dlhost'));
    tearDown(() => dir.deleteSync(recursive: true));

    test('downloads from Qobuz, tags file, and writes history.json', () async {
      final dlDir = Directory.systemTemp.createTempSync('mai_an_dl_out');
      addTearDown(() => dlDir.deleteSync(recursive: true));
      final cfgStore = ConfigStore(dir.path);
      final cfgCtrl = await ConfigController.load(cfgStore);
      await cfgCtrl.update({
        'downloads': {'folder': dlDir.path},
        'qobuz': {'email_or_userid': 'u1', 'password_or_token': 'tok'},
      });
      final lib = LibraryController(
        () => LibraryRepository.open('${dir.path}/library.db', writable: true),
      );
      final history = DownloadHistoryStore(dir.path);
      var tagged = false;

      final fakeClient = _FakeSourceClient(
        source: 'qobuz',
        metadataHandler: (id, mtype, _, _) async => {
          'id': id,
          'title': 'Archangel',
          'track_number': 1,
          'media_number': 1,
          'performer': {'name': 'Burial'},
          'album': {
            'id': 'alb1',
            'title': 'Untrue',
            'artist': {'name': 'Burial'},
            'tracks_count': 13,
            'media_count': 1,
            'release_date_original': '2007-11-05',
          },
        },
      );

      final host = AppDownloadHost(
        dataDir: dir.path,
        library: lib,
        haptics: Haptics(const HapticsConfig(enabled: false)),
        onSnack: (_, {icon, color}) {},
        config: cfgCtrl,
        history: history,
        clientFactory: (_, _) => fakeClient,
        downloadDirOverride: () async => dlDir.path,
        isAppPrivateOverride: (_) => false,
        tagWriter: (path, meta, cover) async {
          tagged = true;
          expect(meta.title, 'Archangel');
          expect(meta.album.album, 'Untrue');
        },
      );
      final queue = DownloadQueue(host);
      queue.enqueue({
        'id': '42',
        'source': 'qobuz',
        'media_type': 'track',
        'name': 'Archangel',
        'artist': 'Burial',
      }, qualityTier: 'mp3');

      await queue.worker;
      await Future<void>.delayed(const Duration(milliseconds: 20));

      expect(tagged, isTrue);
      expect(queue.history.single.state, JobState.done);
      expect(File('${dlDir.path}/01. Archangel.mp3').existsSync(), isTrue);
      final hist = await history.load();
      expect(hist.length, 1);
      expect(hist.first['url'], 'https://www.qobuz.com/track/42');

      lib.dispose();
    });
  });

  group('SearchScreen Phase 6c UI, Previews & Redownload', () {
    late Directory dir;
    late String dbPath;

    setUp(() async {
      dir = Directory.systemTemp.createTempSync('mai_an_search6c');
      dbPath = '${dir.path}/library.db';
      final repo = await LibraryRepository.open(dbPath, writable: true);
      await repo.debugExecute([for (final s in _setup) (s, const [])]);
      await repo.close();
    });

    tearDown(() => dir.deleteSync(recursive: true));

    testWidgets(
      'full Search UI: creds banner, unified hero/shelves, source cache, pagination, expansion, multi-select, preview & cancel',
      (tester) async {
        tester.view.physicalSize = const Size(430 * 2, 960 * 2);
        tester.view.devicePixelRatio = 2;
        addTearDown(tester.view.reset);

        final lib = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await tester.runAsync(lib.load);
        final landing = LandingController(lib);
        final cfgCtrl = await tester.runAsync(
          () => ConfigController.load(ConfigStore(dir.path)),
        );
        final prefs = Prefs(dir.path);
        await tester.runAsync(prefs.load);

        final backend = _FakePlaybackBackend();
        final playerCtrl = PlayerController(
          backend: backend,
          store: QueueStore(dir.path),
          prefs: prefs,
          libraryTracks: () async => lib.allTracks,
        );
        await tester.runAsync(playerCtrl.init);
        final playerNotifier = ValueNotifier<PlayerController?>(playerCtrl);

        // Build 40 tracks so Tracks mode spans 2 pages (35 + 5).
        final qobuzClient = _FakeSourceClient(
          source: 'qobuz',
          searchHandler: (mtype, query) async {
            if (mtype == 'track') {
              return [
                {
                  'tracks': {
                    'items': [
                      _qobuzTrack('t1', 'Archangel', 'Burial'),
                      for (var i = 2; i <= 40; i++)
                        _qobuzTrack('t$i', 'Track $i', 'Burial'),
                    ],
                  },
                },
              ];
            }
            if (mtype == 'album') {
              return [
                {
                  'albums': {
                    'items': [
                      _qobuzAlbum('a1', 'Untrue', 'Burial', tracksCount: 10),
                      _qobuzAlbum('a2', 'Burial', 'Burial', tracksCount: 13),
                    ],
                  },
                },
              ];
            }
            if (mtype == 'artist') {
              return [
                {
                  'artists': {
                    'items': [
                      _qobuzArtist('ar1', 'Burial', 8),
                      _qobuzArtist('ar2', 'Burial Ground', 2),
                    ],
                  },
                },
              ];
            }
            return const [];
          },
          metadataHandler: (id, mtype, limit, offset) async {
            if (mtype == 'artist') {
              return {
                'albums': {
                  'items':
                      (offset ?? 0) == 0
                          ? [_qobuzAlbum('a99', 'Kindred EP', 'Burial', tracksCount: 3)]
                          : const <Object?>[],
                },
              };
            }
            if (mtype == 'album') {
              return {
                'id': id,
                'title': 'Kindred EP',
                'tracks': {
                  'items': [_qobuzTrack('kt1', 'Loner', 'Burial')],
                },
              };
            }
            return const {};
          },
        );
        final deezerClient = _FakeSourceClient(
          source: 'deezer',
          searchHandler: (mtype, query) async {
            if (mtype == 'track') {
              return [
                {
                  'data': [
                    {
                      'id': 901,
                      'title': 'Deezer Archangel',
                      'artist': {'name': 'Burial'},
                      'album': {'title': 'Untrue', 'cover_medium': ''},
                    },
                  ],
                },
              ];
            }
            return const [];
          },
        );

        final searcher = StreamripSearcher(
          readConfig: () async => cfgCtrl!.config.sources,
          clientFactory:
              (source, _) => source == 'deezer' ? deezerClient : qobuzClient,
        );
        final dlHost = _FakeDownloadHost();
        final queue = DownloadQueue(dlHost)..holdWorker = true;
        final openedSettings = <SettingsPage?>[];

        final searchCtrl = SearchTabController(
          repository: () => lib.repository,
          searcher: searcher,
          config: cfgCtrl,
          prefs: prefs,
          player: playerNotifier,
          haptics: Haptics(const HapticsConfig(enabled: false)),
          dataDir: dir.path,
          previewStreamTimeout: const Duration(milliseconds: 500),
        );

        await tester.pumpWidget(
          MaterialApp(
            theme: Tokens.theme(),
            home: Scaffold(
              body: SearchScreen(
                landing: landing,
                controller: searchCtrl,
                downloads: queue,
                onPlayTrack: (_) {},
                onOpenInLibrary: (_, _) {},
                onOpenSettings: ([p]) => openedSettings.add(p),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();

        // 1. Missing-credentials banner shown on landing before creds exist.
        expect(find.text('Streaming Setup'), findsOneWidget);
        await tester.tap(find.text('Connect'));
        expect(openedSettings, [SettingsPage.account]);

        // 2. Configure Qobuz + Deezer credentials; banner disappears.
        await tester.runAsync(
          () => cfgCtrl!.update({
            'qobuz': {'email_or_userid': 'uid', 'password_or_token': 'tok'},
            'deezer': {'arl': 'arl_cookie'},
          }),
        );
        await tester.pumpAndSettle();
        expect(find.text('Streaming Setup'), findsNothing);

        // 3. Submit query -> Unified All view renders Hero + Top Songs + Albums + Artists + ownership badges.
        await tester.enterText(find.byType(TextField), 'Burial');
        await tester.runAsync(() => searchCtrl.search('Burial'));
        await tester.pumpAndSettle();

        expect(searchCtrl.remoteError, isNull);
        expect(find.text('TOP RESULT · ARTIST'), findsOneWidget);
        expect(find.text('Top Songs'), findsOneWidget);
        expect(find.text('Albums & EPs'), findsOneWidget);
        expect(find.text('IN LIBRARY'), findsWidgets);

        // 4. Switch to Deezer and back to Qobuz (Qobuz served from cache!).
        final qCallsBefore = qobuzClient.searchCalls;
        await tester.runAsync(() => searchCtrl.setSource('deezer'));
        await tester.pumpAndSettle();
        expect(find.text('Deezer Archangel'), findsWidgets);

        await tester.runAsync(() => searchCtrl.setSource('qobuz'));
        await tester.pumpAndSettle();
        expect(qobuzClient.searchCalls, qCallsBefore); // cached!
        expect(find.text('TOP RESULT · ARTIST'), findsOneWidget);

        // 5. Switch to Tracks mode -> 2-page pagination bar appears.
        await tester.tap(find.text('Tracks'));
        await tester.pumpAndSettle();
        expect(find.text('Page 1 of 2'), findsOneWidget);
        await tester.tap(find.byTooltip('Next Page'));
        await tester.pumpAndSettle();
        expect(find.text('Page 2 of 2'), findsOneWidget);
        expect(find.text('Track 36'), findsOneWidget);

        // 6. Multi-select via long-press -> Select all -> Download batch -> Quality sheet.
        await tester.longPress(find.text('Track 36'));
        await tester.pumpAndSettle();
        expect(find.text('1 selected'), findsOneWidget);
        await tester.tap(find.text('Select all'));
        await tester.pumpAndSettle();
        expect(find.text('5 selected'), findsOneWidget); // 5 items on page 2
        await tester.tap(find.text('Download'));
        await tester.pumpAndSettle();
        expect(find.text('Select Quality'), findsOneWidget);
        expect(find.text('5 items'), findsOneWidget);
        await tester.tap(find.text('CD Quality'));
        await tester.pumpAndSettle();
        expect(queue.pending.length, 5);
        expect(searchCtrl.selectionMode, isFalse);

        // 7. Stream preview playback (direct Qobuz URL) and stop.
        await tester.runAsync(
          () => searchCtrl.togglePreview(35, searchCtrl.tracks[35]),
        );
        await tester.pumpAndSettle();
        expect(playerCtrl.nowPlaying?.title, '(Preview) Track 36');
        expect(find.byIcon(Icons.pause_circle_filled_rounded), findsOneWidget);
        searchCtrl.cancelPreview();
        await tester.pumpAndSettle();
        expect(find.byIcon(Icons.pause_circle_filled_rounded), findsNothing);

        // 8. Artists tab: expand artist -> Kindred EP + Load More Albums -> expand album -> Loner.
        await tester.tap(find.text('Artists'));
        await tester.pumpAndSettle();
        await tester.runAsync(() => searchCtrl.toggleNode(searchCtrl.artists.first));
        await tester.pumpAndSettle();
        expect(find.text('Kindred EP'), findsOneWidget);
        expect(find.text('Load More Albums'), findsOneWidget);
        final kindred = searchCtrl.currentPageRows().firstWhere(
          (r) => r.item['ui_title'] == 'Kindred EP',
        );
        await tester.runAsync(() => searchCtrl.toggleNode(kindred.item));
        await tester.pumpAndSettle();
        expect(find.text('Loner'), findsOneWidget);

        await tester.runAsync(searcher.closeAll);
        searchCtrl.dispose();
        landing.dispose();
        playerCtrl.dispose();
        lib.dispose();
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
      },
    );

    testWidgets(
      'Library long-press -> Redownload switches to Search tab on Tracks & runs search',
      (tester) async {
        tester.view.physicalSize = const Size(420 * 2, 900 * 2);
        tester.view.devicePixelRatio = 2;
        addTearDown(tester.view.reset);

        final lib = LibraryController(
          () => LibraryRepository.open(dbPath, writable: true),
        );
        await tester.runAsync(lib.load);
        final browse = BrowseController(
          lib,
          views: const [LibraryView.tracks],
        );
        final landing = LandingController(lib);
        final player = ValueNotifier<PlayerController?>(null);
        final searchCtrl = SearchTabController(repository: () => lib.repository);

        await tester.pumpWidget(
          MaterialApp(
            theme: Tokens.theme(),
            home: AppShell(
              library: lib,
              browse: browse,
              landing: landing,
              player: player,
              haptics: Haptics(const HapticsConfig(enabled: false)),
              startTab: AppTab.library,
              searchController: searchCtrl,
            ),
          ),
        );
        await tester.pumpAndSettle();

        await tester.longPress(find.text('Archangel'));
        await tester.pumpAndSettle();
        expect(find.text('Redownload (Different Quality)'), findsOneWidget);

        await tester.tap(find.text('Redownload (Different Quality)'));
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 80)),
        );
        await tester.pumpAndSettle();

        final shell = tester.state<AppShellState>(find.byType(AppShell));
        expect(shell.tab, AppTab.search);
        expect(searchCtrl.activeQuery, 'Archangel Burial');
        expect(searchCtrl.viewMode, SearchViewMode.tracks);

        searchCtrl.dispose();
        landing.dispose();
        browse.dispose();
        lib.dispose();
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 50)),
        );
      },
    );
  });

  group('Jarvis search_online tool hook', () {
    test('returns top 5 slim results and gates download behind confirmation', () async {
      final client = _FakeSourceClient(
        source: 'qobuz',
        searchHandler: (mtype, query) async {
          if (mtype == 'track') {
            return [
              {
                'tracks': {
                  'items': [
                    for (var i = 1; i <= 7; i++)
                      _qobuzTrack('$i', 'Song $i', 'Burial'),
                  ],
                },
              },
            ];
          }
          return const [];
        },
      );
      final searcher = StreamripSearcher(
        readConfig: () async => const SourceConfig(),
        clientFactory: (_, _) => client,
      );
      final triggered = <Map<String, Object?>>[];
      final handler = buildOnlineSearchHandler(
        searcher: searcher,
        onDownloadItem: (item) async => triggered.add(item),
      );
      final runner = AssistantRunner(
        library: _DummyAssistantLibrary(),
        player: _DummyAssistantPlayer(),
        onlineSearchHandler: handler,
      );

      // Empty query
      final empty = await executeTool('search_online', {'query': ''}, runner);
      expect(empty['success'], isFalse);

      // Search only: capped to 5 slim entries
      final res = await executeTool(
        'search_online',
        {'query': 'Burial'},
        runner,
      );
      expect(res['success'], isTrue);
      final results = res['results']! as List;
      expect(results.length, 5);
      expect((results.first as Map)['title'], 'Song 1');
      expect((results.first as Map)['url'], 'https://www.qobuz.com/track/1');

      // Download=true: arms confirmation instead of downloading immediately
      final gated = await executeTool(
        'search_online',
        {'query': 'Burial', 'download': true},
        runner,
      );
      expect(gated['awaiting_confirmation'], isTrue);
      expect(triggered, isEmpty);

      // Confirming ("yes") executes the armed download callback
      final confirmed = await runner.dispatchText('yes');
      expect(confirmed.success, isTrue);
      expect(confirmed.displayed, contains('Started download: **Song 1**'));
      expect(triggered.length, 1);
      expect(triggered.first['url'], 'https://www.qobuz.com/track/1');
    });
  });
}
