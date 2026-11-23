import 'dart:math' as math;
import 'dart:typed_data';

import 'artist_graph.dart';
import 'credit_keys.dart';

/// Stations over the library: the Dart counterpart of `track_graph`'s
/// coordinate graph (`load_live_coordinate_graph`), its artist-graph cache
/// (`_artist_graph_cached`), `Station`, `open_station` and `walk`.

/// One `play_counts` row with coordinates, joined with its display metadata
/// (`db_manager.get_tracks_pca_coords`).
class StationRow {
  const StationRow({
    required this.path,
    required this.coords,
    this.bpm = 0,
    this.energy = 0,
    this.title,
    this.artist,
    this.album,
  });
  final String path;

  /// The float32 `pca_coords` blob.
  final Uint8List coords;
  final double bpm;
  final double energy;
  final String? title;
  final String? artist;
  final String? album;
}

/// `db_manager.get_artist_meta_for_paths`, one path's entry.
class ArtistMeta {
  const ArtistMeta({
    this.artist,
    this.country,
    this.genres = const [],
    this.manual = false,
  });
  final String? artist;
  final String? country;
  final List<String> genres;
  final bool manual;
}

/// Everything the stations need, read in one database job.
class StationData {
  const StationData({
    required this.rows,
    this.enrichment = const {},
    this.npmi = const {},
  });
  final List<StationRow> rows;
  final Map<String, ArtistMeta> enrichment;
  final Map<String, double> npmi;
}

/// The coordinate graph and the artist graph built over it. Built once per
/// library load (off the UI isolate); stations opened from it are cheap.
class StationSource {
  StationSource._({
    required this.paths,
    required this.dim,
    required this.xUnit,
    required this.bpm,
    required this.energy,
    required this.titles,
    required this.artists,
    required this.albums,
    required this.graph,
    required this.artistKeys,
    required this.skipped,
  }) : indexOf = {for (var i = 0; i < paths.length; i++) paths[i]: i};

  final List<String> paths;
  final Map<String, int> indexOf;
  final int dim;

  /// (N, dim) L2-normalised coordinates, row-major.
  final Float32List xUnit;
  final Float64List bpm;
  final Float64List energy;
  final List<String?> titles;
  final List<String?> artists;
  final List<String?> albums;
  final ArtistGraph graph;

  /// `_credit_key_set` of each track's display artist.
  final List<Set<String>> artistKeys;

  /// Rows dropped because their blob was empty or had another dimension.
  final int skipped;

  int get length => paths.length;
  bool contains(String path) => indexOf.containsKey(path);

  factory StationSource.build(StationData data) {
    var dim = 0;
    for (final r in data.rows) {
      final n = r.coords.lengthInBytes;
      if (n >= 4 && n % 4 == 0) {
        dim = n ~/ 4;
        break;
      }
    }
    final kept = [
      for (final r in data.rows)
        if (dim > 0 && r.coords.lengthInBytes == dim * 4) r,
    ];
    final x = Float32List(kept.length * dim);
    final row = Float32List(dim);
    for (var i = 0; i < kept.length; i++) {
      final bytes = ByteData.sublistView(kept[i].coords);
      var norm = 0.0;
      for (var d = 0; d < dim; d++) {
        // numpy writes native-endian float32; every target we ship is LE.
        row[d] = bytes.getFloat32(d * 4, Endian.little);
        norm += row[d] * row[d];
      }
      final n32 = Float32List.fromList([math.sqrt(norm)])[0];
      final div = math.max(n32, 1e-9);
      for (var d = 0; d < dim; d++) {
        x[i * dim + d] = row[d] / div;
      }
    }
    final keys = CreditKeyCache();
    final meta = <TrackMeta>[];
    for (final r in kept) {
      final e = data.enrichment[r.path];
      meta.add(
        TrackMeta(
          artist: _truthy(e?.artist) ?? r.artist,
          credit: r.artist,
          genres: e?.genres ?? const [],
          country: e?.country,
          manual: e?.manual ?? false,
          names: [r.artist, r.album, r.title],
        ),
      );
    }
    final graph = ArtistGraph(
      x,
      dim,
      meta,
      npmi: data.npmi,
      creditKeys: keys.call,
    );
    return StationSource._(
      paths: [for (final r in kept) r.path],
      dim: dim,
      xUnit: x,
      bpm: Float64List.fromList([for (final r in kept) r.bpm]),
      energy: Float64List.fromList([for (final r in kept) r.energy]),
      titles: [for (final r in kept) r.title],
      artists: [for (final r in kept) r.artist],
      albums: [for (final r in kept) r.album],
      graph: graph,
      artistKeys: [for (final r in kept) keys(r.artist)],
      skipped: data.rows.length - kept.length,
    );
  }

  /// `open_station`: a station from [seed]. [focused] keeps it under the
  /// seed's genre umbrella. Repeat caps count over the last [window] emitted
  /// tracks (0 = the whole station). [avoid] is never emitted. A seed without
  /// coordinates gives an empty station.
  Station open(
    String seed, {
    Iterable<String> avoid = const [],
    bool focused = false,
    int maxPerArtist = 2,
    int maxPerAlbum = 1,
    int window = 0,
  }) {
    final s = indexOf[seed];
    if (s == null) return Station._(null, this);
    return Station._(
      Drift(
        graph,
        xUnit,
        s,
        exclude: [for (final p in avoid) ?indexOf[p]],
        artistKeys: artistKeys,
        albumKeys: albums,
        caps: RollingCaps(
          maxPerArtist: maxPerArtist,
          maxPerAlbum: maxPerAlbum,
          window: window,
        ),
        focused: focused,
        bpm: bpm,
        energy: energy,
      ),
      this,
    );
  }

  /// `walk`: a station's first [length] tracks. Deterministic.
  List<String> walk(
    String seed, {
    int length = 10,
    Iterable<String> avoid = const [],
    int maxPerArtist = 2,
    int maxPerAlbum = 1,
    bool focused = false,
  }) => open(
    seed,
    avoid: avoid,
    focused: focused,
    maxPerArtist: maxPerArtist,
    maxPerAlbum: maxPerAlbum,
  ).take(length);
}

String? _truthy(String? s) => (s == null || s.isEmpty) ? null : s;

/// What auto-play holds of a station (`track_graph.Station`'s surface).
abstract interface class StationHandle {
  /// Display name of the genre the station is playing now, or null.
  String? get flavour;
  List<String> take(int n);
  void exclude(Iterable<String> paths);
  void accept(String path);
  void reject(Iterable<String> paths);
}

/// A resumable similarity station from one seed track. Every [take]
/// continues where the last stopped and never emits a track twice.
class Station implements StationHandle {
  Station._(this._d, this._src);
  final Drift? _d;
  final StationSource _src;

  bool get isEmpty => _d == null;

  @override
  String? get flavour => _d?.flavour();

  @override
  List<String> take(int n) =>
      _d == null ? const [] : [for (final i in _d.take(n)) _src.paths[i]];

  List<int> _idx(Iterable<String> paths) => [
    for (final p in paths) ?_src.indexOf[p],
  ];

  /// Never emit these (e.g. tracks played from elsewhere meanwhile).
  @override
  void exclude(Iterable<String> paths) => _d?.exclude(_idx(paths));

  /// A track listened through: its artist pulls the drift.
  @override
  void accept(String path) {
    final i = _src.indexOf[path];
    if (i != null) _d?.accept(i);
  }

  /// Never emit [paths], and steer away from their artists' neighbourhood.
  @override
  void reject(Iterable<String> paths) => _d?.reject(_idx(paths));
}
