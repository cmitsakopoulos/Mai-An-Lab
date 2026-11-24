import '../data/library_repository.dart';
import '../data/network_queries.dart' show kEdgeKindArtist;
import '../player/player_controller.dart';
import '../player/queue_entry.dart';
import '../stations/station.dart';
import 'runner.dart';

/// Concrete [AssistantLibrary] over [LibraryRepository] and [StationSource]
/// (phase 5, stage c2).
///
/// [stations] returns the cached coordinate/artist graph (for example
/// `() async => libraryController.stations ?? await repo.stations()`). When
/// omitted, [repo.stations] is loaded on demand.
class RepoAssistantLibrary implements AssistantLibrary {
  RepoAssistantLibrary(this.repo, {Future<StationSource?> Function()? stations})
    : _stations = stations ?? (() => repo.stations());

  final LibraryRepository repo;
  final Future<StationSource?> Function() _stations;

  @override
  Future<int> getTotalTracks() => repo.totalTracks();

  @override
  Future<List<Row>> searchTracksSimple(String query, {required int limit}) =>
      repo.searchTracksSimple(query, limit: limit);

  @override
  Future<List<Row>> getAllTracks({String searchQuery = ''}) =>
      repo.allTracksRaw(searchQuery: searchQuery);

  @override
  Future<Row?> getTrackFull(String path) => repo.trackFull(path);

  @override
  Future<bool> hasCurrentFeatures(String path) => repo.hasCurrentFeatures(path);

  @override
  Future<Set<String>> recentPlayedPaths({required int windowSeconds}) =>
      repo.recentPlayedPaths(windowSeconds: windowSeconds);

  @override
  Future<void> recordPlayback(String path, String event, {String? seedPath}) =>
      repo.recordPlayback(path, event, seedPath: seedPath);

  @override
  Future<List<Row>> getMostPlayed({required int limit}) =>
      repo.mostPlayedFull(limit);

  @override
  Future<List<Row>> getAllArtists({String searchQuery = ''}) =>
      repo.allArtistsRaw(searchQuery: searchQuery);

  @override
  Future<List<String>> searchArtistNames(String query) =>
      repo.searchArtistNames(query);

  @override
  Future<List<Row>> getAllAlbums({String searchQuery = ''}) =>
      repo.allAlbumsRaw(searchQuery: searchQuery);

  @override
  Future<List<Row>> getAlbumsByArtist(String artist) =>
      repo.albumsByArtistRaw(artist);

  @override
  Future<List<Row>> getTracksByAlbum(String album, String artist) =>
      repo.tracksByAlbumRaw(album, artist);

  @override
  Future<List<Row>> getAllPlaylists({String searchQuery = ''}) =>
      repo.allPlaylistsRaw(searchQuery: searchQuery);

  @override
  Future<List<Row>> getTracksInPlaylist(int playlistId) =>
      repo.tracksInPlaylistRaw(playlistId);

  @override
  Future<List<Row>> getRecentTracks({required int limit}) =>
      repo.recentTracksRaw(limit);

  @override
  Future<String?> firstTrackPathByArtist(String name) =>
      repo.firstTrackPathByArtist(name);

  @override
  Future<int> createPlaylist(String name) => repo.createPlaylist(name);

  @override
  Future<void> addTrackToPlaylist(int playlistId, String path) =>
      repo.addTracksToPlaylist(playlistId, [path]);

  @override
  Future<Row?> getArtistEnrichment(String name) => repo.artistEnrichment(name);

  @override
  Future<Row?> getAiResearch(String artistName) => repo.aiResearch(artistName);

  @override
  Future<Map<String, Object?>> getPendingAiResearch() =>
      repo.pendingAiResearch();

  @override
  Future<List<String>> getArtistSourceGenres(String artistName) =>
      repo.artistSourceGenres(artistName);

  @override
  Future<List<Row>> getMetadataGapArtists({required int limit}) =>
      repo.metadataGapArtists(limit: limit);

  @override
  Future<Row> getMetadataCoverage({List<Row>? gaps}) =>
      repo.metadataCoverage(gaps: gaps);

  @override
  Future<List<String>> walk(
    String seedPath, {
    required int length,
    required Set<String> avoid,
  }) async {
    final src = await _stations();
    if (src == null) return const [];
    return src.walk(seedPath, length: length, avoid: avoid);
  }

  @override
  Future<List<String>> artistNeighbors(
    String seedPath, {
    required int k,
  }) async {
    final rows = await repo.networkNeighbors(
      seedPath,
      k,
      edgeKind: kEdgeKindArtist,
    );
    return [for (final r in rows) r.path];
  }
}

/// Concrete [AssistantPlayer] over [PlayerController] (phase 5, stage c2).
///
/// Mirrors Flet's `AudioEngine` surface: `setQueue` pushes and autoplays
/// immediately (deferred play is a no-op there), `isShuffle =` switches the
/// play order in place (`setShuffleOrder`, not `toggleShuffle`), and a
/// stopped controller reports no active track metadata, as `audio_engine.stop`
/// clears its current-track strings.
class ControllerAssistantPlayer implements AssistantPlayer {
  ControllerAssistantPlayer(this.controller);

  final PlayerController controller;

  QueueEntry? get _active =>
      controller.stopped ? null : controller.queue.current;

  @override
  List<Row> get queue => [
    for (final e in controller.queue.entries) Map<String, Object?>.of(e.data),
  ];

  @override
  int get currentIndex => controller.queue.currentIndex;

  @override
  String get currentPath => _active?.path ?? '';

  @override
  String get currentTrack => _active?.title ?? '';

  @override
  String get currentArtist => _active?.artist ?? '';

  @override
  String get currentAlbum => _active?.album ?? '';

  @override
  double get duration {
    final active = _active;
    if (active == null) return 0.0;
    if (controller.duration > Duration.zero) {
      return controller.duration.inMilliseconds / 1000.0;
    }
    return active.duration;
  }

  @override
  bool get isPlaying => controller.playing;

  @override
  bool get isShuffle => controller.shuffle;

  @override
  set isShuffle(bool value) => controller.setShuffleOrder(value);

  @override
  void setQueue(List<Row> tracks, {int startIndex = 0}) =>
      controller.setQueueEntries([
        for (final t in tracks) QueueEntry(Map<String, Object?>.of(t)),
      ], startIndex: startIndex);

  @override
  void queueLast(Row track) =>
      controller.queueLastEntry(QueueEntry(Map<String, Object?>.of(track)));

  @override
  void queueNext(Row track) =>
      controller.queueNextEntry(QueueEntry(Map<String, Object?>.of(track)));

  @override
  void queueLastMany(List<Row> tracks) => controller.queueLastEntries([
    for (final t in tracks) QueueEntry(Map<String, Object?>.of(t)),
  ]);

  @override
  void removeFromQueue(int index) => controller.removeAt(index);

  @override
  void moveQueueItem(int oldIndex, int newIndex) =>
      controller.moveRow(oldIndex, newIndex);

  @override
  void next() => controller.next();

  @override
  void previous() => controller.previous();

  @override
  void pause() => controller.pause();

  @override
  void play() => controller.play();

  @override
  void stop() => controller.stop();

  @override
  void clearQueue() => controller.clearQueue();
}
