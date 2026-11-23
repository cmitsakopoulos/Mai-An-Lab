import 'dart:collection';
import 'dart:math' as math;
import 'dart:typed_data';

import '../core/py_text.dart';
import 'genre_taxonomy.dart';

/// Artist-affinity stations: a port of `utils/artist_graph.py` (what auto-play
/// and "play similar" queue next). The Python module explains every constant
/// and rule; this file keeps its structure so the two can be read side by side.
///
/// Numbers: numpy keeps the matrices in float32. They are stored here in
/// [Float32List]s too, so inputs round the same way; sums run in double, so
/// results agree to float32 precision, not bit for bit.

// ── Tag similarity ──────────────────────────────────────────────────────────

const familySim = 0.3;
const rootSim = 0.6;
const _minRoot = 3;

bool _isCompoundOf(String tag, String head) =>
    head.runes.length >= _minRoot &&
    tag.runes.length - head.runes.length >= _minRoot &&
    tag.endsWith(head);

String? _family(String tag) {
  final f = genreBucket(tag);
  return nonFamilies.contains(f) ? null : f;
}

/// (T, T) similarity in [0, 1] between canonical genre tokens, row-major.
Float32List tagSimilarity(List<String> tags, [Map<String, double>? npmi]) {
  final t = tags.length;
  final s = Float32List(t * t);
  final fams = [for (final x in tags) _family(x)];
  final regional = [for (final x in tags) isRegionalTag(x)];
  for (var i = 0; i < t; i++) {
    final a = tags[i];
    for (var j = i + 1; j < t; j++) {
      final b = tags[j];
      var v = 0.0;
      if (fams[i] != null && fams[i] == fams[j] && regional[i] == regional[j]) {
        v = familySim;
      }
      if (_isCompoundOf(a, b) || _isCompoundOf(b, a)) v = math.max(v, rootSim);
      if (npmi != null && npmi.isNotEmpty) {
        final key = a.compareTo(b) < 0 ? '$a|$b' : '$b|$a';
        v = math.max(v, npmi[key] ?? 0.0);
      }
      s[i * t + j] = v;
      s[j * t + i] = v;
    }
    s[i * t + i] = 1.0;
  }
  return s;
}

// ── Culture ─────────────────────────────────────────────────────────────────

const regionalCountries = {'GR'};

/// The regional culture an artist belongs to, or null.
String? artistCulture(
  String? country,
  Iterable<String> genres,
  Iterable<String?> names, {
  Set<String> regional = regionalCountries,
}) {
  final c = (country ?? '').trim().toUpperCase();
  if (regional.contains(c)) return c;
  for (final tok in genres) {
    final cu = tagCulture(tok);
    if (regional.contains(cu)) return cu;
  }
  final cu = scriptCulture(names);
  return regional.contains(cu) ? cu : null;
}

bool isCultureWord(String tok) =>
    cultureWords.contains(alnumOnly(pyLower(tok)));

// ── The artist graph ────────────────────────────────────────────────────────

const crossCulture = 0.35;
const collabSim = 0.9;
const inferredDiscount = 0.8;
const donors = 5;
const borrowShare = 0.5;
const cultureMinLinks = 2;
const culturePasses = 2;

/// One track's metadata, as `track_graph._artist_graph_cached` builds it.
class TrackMeta {
  const TrackMeta({
    this.artist,
    this.credit,
    this.genres = const [],
    this.country,
    this.manual = false,
    this.names = const [],
  });

  /// The enrichment (album) artist.
  final String? artist;

  /// The track's own artist credit.
  final String? credit;

  /// Canonical genre tokens of the artist (empty when unknown).
  final List<String> genres;
  final String? country;
  final bool manual;

  /// (artist, album, title).
  final List<String?> names;
}

typedef CreditKeysFn = Set<String> Function(String? name);

Set<String> _defaultKeys(String? name) =>
    (name == null || name.isEmpty) ? const {} : {pyLower(name)};

double _f32(double x) {
  _f32buf[0] = x;
  return _f32buf[0];
}

final _f32buf = Float32List(1);

final _crossCulture32 = _f32(crossCulture);
final _inferred32 = _f32(inferredDiscount);
final _acousticW32 = _f32(acousticW);

/// Artists, their soft genre profiles and the affinity between them.
class ArtistGraph {
  ArtistGraph(
    Float32List xUnit,
    this.dim,
    List<TrackMeta> meta, {
    Map<String, double>? npmi,
    CreditKeysFn creditKeys = _defaultKeys,
  }) {
    final n = meta.length;
    final names = [
      for (var i = 0; i < n; i++)
        _truthy(meta[i].artist) ?? _truthy(meta[i].credit) ?? '#$i',
    ];
    final uidx = <String, int>{};
    for (final name in names) {
      uidx.putIfAbsent(name, () => uidx.length);
    }
    units = uidx.keys.toList();
    final u = units.length;
    unitOf = Int32List.fromList([for (final name in names) uidx[name]!]);
    final members = List.generate(u, (_) => <int>[]);
    for (var i = 0; i < n; i++) {
      members[unitOf[i]].add(i);
    }
    tracksOf = [for (final m in members) Int32List.fromList(m)];

    // Same act (shared member key) and collaboration links.
    final unitKeys = [for (final name in units) creditKeys(name)];
    final byKey = <String, Set<int>>{};
    for (var k = 0; k < u; k++) {
      for (final key in unitKeys[k]) {
        (byKey[key] ??= {}).add(k);
      }
    }
    sameAct = List.generate(u, (_) => <int>{});
    for (final us in byKey.values) {
      for (final a in us) {
        sameAct[a].addAll(us.where((b) => b != a));
      }
    }
    collab = List.generate(u, (_) => <int>{});
    for (var i = 0; i < n; i++) {
      final touched = <int>{
        for (final key in creditKeys(meta[i].credit ?? '')) ...?byKey[key],
        unitOf[i],
      };
      if (touched.length > 1) {
        for (final a in touched) {
          collab[a].addAll(touched.where((b) => b != a));
        }
      }
    }

    // Profiles and culture.
    final profiles = <List<String>>[];
    final ownCulture = <String?>[];
    final pinned = List.filled(u, false);
    final manualU = List.filled(u, false);
    final foreignRow = List.filled(u, false);
    for (var k = 0; k < u; k++) {
      final idxs = members[k];
      manualU[k] = idxs.any((i) => meta[i].manual);
      final derived =
          unitKeys[k].length > 1 && sameAct[k].isNotEmpty && !manualU[k];
      final genres = derived
          ? const <String>[]
          : idxs
                .map((i) => meta[i].genres)
                .firstWhere((g) => g.isNotEmpty, orElse: () => const []);
      final country = idxs
          .map((i) => meta[i].country)
          .firstWhere((c) => c != null && c.isNotEmpty, orElse: () => null);
      profiles.add(
        ({
          for (final t in genres)
            if (!isCultureWord(t)) t,
        }.toList()..sort()),
      );
      final texts = [
        for (final i in idxs)
          for (final x in meta[i].names)
            if (x != null && x.isNotEmpty) x,
      ];
      ownCulture.add(artistCulture(country, genres, texts));
      pinned[k] = manualU[k] && country != null;
      foreignRow[k] = country != null && !derived;
    }
    culture = _spreadCulture(ownCulture, pinned);
    namesake = [
      for (var k = 0; k < u; k++)
        foreignRow[k] && !manualU[k] && culture[k] != ownCulture[k],
    ];
    for (var k = 0; k < u; k++) {
      if (namesake[k]) profiles[k] = const [];
    }

    // Artist centroids in sound.
    centroids = Float32List(u * dim);
    for (var k = 0; k < u; k++) {
      final acc = Float32List(dim);
      final tr = tracksOf[k];
      for (final i in tr) {
        for (var d = 0; d < dim; d++) {
          acc[d] += xUnit[i * dim + d];
        }
      }
      var norm = 0.0;
      for (var d = 0; d < dim; d++) {
        acc[d] = acc[d] / tr.length;
        norm += acc[d] * acc[d];
      }
      final inv = math.max(math.sqrt(norm), 1e-9);
      for (var d = 0; d < dim; d++) {
        centroids[k * dim + d] = acc[d] / inv;
      }
    }

    // Tag profiles.
    tags = {for (final p in profiles) ...p}.toList()..sort();
    final t = tags.length;
    final tidx = {for (var k = 0; k < t; k++) tags[k]: k};
    var w = Float32List(u * t);
    for (var k = 0; k < u; k++) {
      for (final tag in profiles[k]) {
        w[k * t + tidx[tag]!] = 1.0;
      }
    }
    final tagged = [for (final p in profiles) p.isNotEmpty];
    inferred = List.filled(u, false);
    hasTags = tagged.contains(true);
    if (hasTags) {
      final untagged = [
        for (var k = 0; k < u; k++)
          if (!tagged[k]) k,
      ];
      for (final k in untagged) {
        var cand = [
          for (var v = 0; v < u; v++)
            if (tagged[v] && (culture[v] ?? '') == (culture[k] ?? '')) v,
        ];
        if (cand.isEmpty) {
          cand = [
            for (var v = 0; v < u; v++)
              if (tagged[v]) v,
          ];
        }
        final sims = [for (final v in cand) _centroidDot(v, k)];
        final order = List.generate(cand.length, (i) => i)
          ..sort((a, b) => sims[b].compareTo(sims[a]));
        final top = order.take(donors).toList();
        final wts = [for (final i in top) math.max(sims[i], 0.0)];
        final total = wts.fold(0.0, (a, b) => _f32(a + b));
        if (total > 0) {
          for (var c = 0; c < t; c++) {
            var acc = 0.0;
            for (var q = 0; q < top.length; q++) {
              acc = _f32(acc + _f32(w[cand[top[q]] * t + c] * wts[q]));
            }
            w[k * t + c] = acc / total;
          }
        }
      }
      for (var pass = 0; pass < 2; pass++) {
        final next = Float32List.fromList(w);
        for (final k in untagged) {
          final links = {...collab[k], ...sameAct[k]}.toList()..sort();
          if (links.isEmpty) continue;
          for (var c = 0; c < t; c++) {
            var acc = 0.0;
            for (final v in links) {
              acc = _f32(acc + w[v * t + c]);
            }
            next[k * t + c] = _f32(acc + w[k * t + c]) / (links.length + 1);
          }
        }
        w = next;
      }
      for (final k in untagged) {
        var top = 0.0;
        for (var c = 0; c < t; c++) {
          top = math.max(top, w[k * t + c]);
        }
        if (top > 0) {
          for (var c = 0; c < t; c++) {
            if (w[k * t + c] < borrowShare * top) w[k * t + c] = 0;
          }
        }
      }
      for (final k in untagged) {
        var sum = 0.0;
        for (var c = 0; c < t; c++) {
          sum += w[k * t + c];
        }
        inferred[k] = sum > 0;
      }
    }
    this.w = w;

    // Rarity-weighted profiles and their similarity products.
    final wi = Float32List.fromList(w);
    if (hasTags) {
      final nTagged = tagged.where((x) => x).length;
      for (var c = 0; c < t; c++) {
        var df = 0;
        for (var k = 0; k < u; k++) {
          if (tagged[k] && w[k * t + c] > 0) df++;
        }
        final idf = _f32(math.log((1.0 + nTagged) / (1.0 + df)) + 1.0);
        for (var k = 0; k < u; k++) {
          wi[k * t + c] = w[k * t + c] * idf;
        }
      }
    }
    final s = tagSimilarity(tags, npmi);
    _wi = wi;
    _nz = [
      for (var k = 0; k < u; k++)
        Int32List.fromList([
          for (var c = 0; c < t; c++)
            if (wi[k * t + c] != 0) c,
        ]),
    ];
    _ws = Float32List(u * t);
    for (var k = 0; k < u; k++) {
      final acc = Float64List(t);
      for (final c in _nz[k]) {
        final a = wi[k * t + c];
        for (var d = 0; d < t; d++) {
          acc[d] += a * s[c * t + d];
        }
      }
      for (var d = 0; d < t; d++) {
        _ws[k * t + d] = acc[d];
      }
    }
    _norm = Float64List(u);
    for (var k = 0; k < u; k++) {
      var acc = 0.0;
      for (final c in _nz[k]) {
        acc += _ws[k * t + c] * wi[k * t + c];
      }
      _norm[k] = _f32(math.sqrt(math.max(_f32(acc), 0.0)));
    }
  }

  late final List<String> units;
  late final Int32List unitOf;
  late final List<Int32List> tracksOf;
  late final List<Set<int>> sameAct;
  late final List<Set<int>> collab;
  late final List<String?> culture;
  late final List<bool> namesake;
  late final List<bool> inferred;
  late final bool hasTags;
  late final List<String> tags;
  final int dim;
  late final Float32List centroids;

  /// (U, T) tag profiles, row-major.
  late final Float32List w;
  late final Float32List _wi;
  late final Float32List _ws;
  late final List<Int32List> _nz;
  late final Float64List _norm;
  final _rows = <int, Float32List>{};

  int get length => units.length;

  double _centroidDot(int a, int b) {
    var dot = 0.0;
    for (var d = 0; d < dim; d++) {
      dot += centroids[a * dim + d] * centroids[b * dim + d];
    }
    return _f32(dot);
  }

  List<String?> _spreadCulture(List<String?> start, List<bool> pinned) {
    var out = List<String?>.of(start);
    for (var pass = 0; pass < culturePasses; pass++) {
      final next = List<String?>.of(out);
      for (var k = 0; k < out.length; k++) {
        final links = {...collab[k], ...sameAct[k]};
        if (out[k] != null || pinned[k] || links.length < cultureMinLinks) {
          continue;
        }
        final votes = <String, int>{};
        for (final v in links) {
          final c = out[v];
          if (c != null && c.isNotEmpty) votes[c] = (votes[c] ?? 0) + 1;
        }
        if (votes.isEmpty) continue;
        final top = votes.entries.reduce((a, b) => b.value > a.value ? b : a);
        if (top.value >= cultureMinLinks && top.value > links.length / 2) {
          next[k] = top.key;
        }
      }
      out = next;
    }
    return out;
  }

  /// Affinity of artist [u] to every artist, in [0, 1] (self = 1).
  Float32List row(int u) {
    final cached = _rows[u];
    if (cached != null) return cached;
    final n = length;
    final t = tags.length;
    final r = Float32List(n);
    if (hasTags) {
      if (_norm[u] > 0) {
        for (var v = 0; v < n; v++) {
          var dot = 0.0;
          for (final c in _nz[v]) {
            dot += _wi[v * t + c] * _ws[u * t + c];
          }
          r[v] = _f32(dot) / _f32(_f32(_norm[v] * _norm[u]) + 1e-9);
        }
      }
      if (inferred[u]) {
        for (var v = 0; v < n; v++) {
          r[v] = r[v] * _inferred32;
        }
      }
      for (var v = 0; v < n; v++) {
        if (inferred[v]) r[v] = r[v] * _inferred32;
      }
    } else {
      for (var v = 0; v < n; v++) {
        r[v] = _f32(_centroidDot(v, u) + 1.0) / 2.0;
      }
    }
    for (var v = 0; v < n; v++) {
      r[v] = r[v].clamp(0.0, 1.0);
    }
    for (final v in collab[u]) {
      r[v] = math.max(r[v], collabSim);
    }
    for (final v in sameAct[u]) {
      r[v] = 1.0;
    }
    r[u] = 1.0;
    _rows[u] = r;
    return r;
  }

  /// Mask of the artists outside artist [u]'s culture.
  List<bool> foreignTo(int u) {
    final c = culture[u] ?? '';
    return [for (final x in culture) (x ?? '') != c];
  }

  /// Display name for the genre a run of artists shares most, or null.
  String? flavour(Iterable<int> unitsRun) {
    final list = unitsRun.toList();
    if (!hasTags || list.isEmpty) return null;
    final t = tags.length;
    final weight = Float64List(t);
    for (final k in list) {
      for (var c = 0; c < t; c++) {
        weight[c] += w[k * t + c];
      }
    }
    var best = 0;
    for (var c = 1; c < t; c++) {
      if (weight[c] > weight[best]) best = c;
    }
    if (t == 0 || weight[best] <= 0) return null;
    return genreDisplayLabel(tags[best]);
  }
}

String? _truthy(String? s) => (s == null || s.isEmpty) ? null : s;

// ── Repeat caps ─────────────────────────────────────────────────────────────

/// Repeat limits over the last [window] emitted tracks (0 = the whole run).
class RollingCaps {
  RollingCaps({this.maxPerArtist = 0, this.maxPerAlbum = 0, int window = 0})
    : _window = window,
      _recent = window > 0 ? Queue() : null;

  final int maxPerArtist;
  final int maxPerAlbum;
  final int _window;
  final Queue<(Set<String>, String?)>? _recent;
  final _artists = <String, int>{};
  final _albums = <String, int>{};

  bool ok(Set<String> keys, String? album) {
    if (maxPerArtist > 0 &&
        keys.any((k) => (_artists[k] ?? 0) >= maxPerArtist)) {
      return false;
    }
    if (maxPerAlbum > 0 &&
        album != null &&
        album.isNotEmpty &&
        (_albums[album] ?? 0) >= maxPerAlbum) {
      return false;
    }
    return true;
  }

  void add(Set<String> keys, String? album) {
    final recent = _recent;
    if (recent != null) {
      if (recent.length >= _window) {
        final (oldKeys, oldAlbum) = recent.removeFirst();
        for (final k in oldKeys) {
          _artists[k] = (_artists[k] ?? 0) - 1;
        }
        if (oldAlbum != null && oldAlbum.isNotEmpty) {
          _albums[oldAlbum] = (_albums[oldAlbum] ?? 0) - 1;
        }
      }
      recent.add((keys, album));
    }
    for (final k in keys) {
      _artists[k] = (_artists[k] ?? 0) + 1;
    }
    if (album != null && album.isNotEmpty) {
      _albums[album] = (_albums[album] ?? 0) + 1;
    }
  }
}

// ── The drift ───────────────────────────────────────────────────────────────

const recentArtists = 5;
const drift = 0.9;
const seedFloor = 0.25;
const repel = 0.5;
const repeatDecay = 0.6;
const repeatWindow = 12;
const acousticW = 0.15;
const minAffinity = 0.1;
const segueTempo = 0.08;
const segueEnergy = 1.0;
const umbrella = 0.4;

/// A bounded FIFO, like `deque(maxlen=…)`.
class _Ring {
  _Ring(this.max, [Iterable<int> init = const []]) {
    init.forEach(add);
  }
  final int max;
  final items = <int>[];
  void add(int x) {
    items.add(x);
    if (items.length > max) items.removeAt(0);
  }
}

/// A resumable station from one seed track over an [ArtistGraph].
class Drift {
  Drift(
    this.g,
    this.x,
    int seedIdx, {
    Iterable<int> exclude = const [],
    this.artistKeys,
    this.albumKeys,
    RollingCaps? caps,
    bool focused = false,
    Float64List? bpm,
    Float64List? energy,
  }) : caps = caps ?? RollingCaps() {
    final n = g.unitOf.length;
    final b = bpm ?? Float64List(n);
    final e = energy ?? Float64List(n);
    _tempo = Float64List.fromList([
      for (final v in b) math.log(math.max(v, 1.0)) / math.ln2,
    ]);
    _timed = [for (final v in b) v > 0];
    _allTimed = !_timed.contains(false);
    var mean = 0.0;
    for (final v in e) {
      mean += v;
    }
    mean = n > 0 ? mean / n : 0.0;
    var varSum = 0.0;
    for (final v in e) {
      varSum += (v - mean) * (v - mean);
    }
    final sd = n > 0 ? math.sqrt(varSum / n) : 0.0;
    final div = sd > 1e-9 ? sd : 1.0;
    _energy = Float64List.fromList([for (final v in e) (v - mean) / div]);
    used = List.filled(n, false);
    this.exclude(exclude);
    used[seedIdx] = true;
    seedUnit = g.unitOf[seedIdx];
    _foreign = g.foreignTo(seedUnit);
    if (focused) {
      final near = g.row(seedUnit);
      _inside = [
        for (var u = 0; u < near.length; u++)
          (_foreign[u] ? _f32(near[u] * _crossCulture32) : near[u]) >= umbrella,
      ];
    }
    _last = seedIdx;
    _recent = _Ring(recentArtists, [seedUnit]);
    _played = _Ring(repeatWindow, [seedUnit]);
  }

  final ArtistGraph g;

  /// (N, D) unit vectors, row-major; D = [ArtistGraph.dim].
  final Float32List x;
  final List<Set<String>>? artistKeys;
  final List<String?>? albumKeys;
  final RollingCaps caps;
  late final List<bool> used;
  late final int seedUnit;
  late final Float64List _tempo;
  late final List<bool> _timed;
  late final bool _allTimed;
  late final Float64List _energy;
  late final List<bool> _foreign;
  List<bool>? _inside;
  late int _last;
  int _n = 0;
  late _Ring _recent;
  late final _Ring _played;
  final _rejected = <int>{};

  List<int> take(int n) {
    final out = <int>[];
    while (out.length < n) {
      final j = _pick();
      if (j == null) break;
      _emit(j);
      out.add(j);
    }
    return out;
  }

  void exclude(Iterable<int> idxs) {
    for (final j in idxs) {
      used[j] = true;
    }
  }

  /// A track listened through: its artist pulls the drift.
  void accept(int idx) {
    if (idx >= 0 && idx < used.length) _recent.add(g.unitOf[idx]);
  }

  /// Never emit these, and steer away from their artists' neighbourhood.
  void reject(Iterable<int> idxs) {
    for (final j in idxs) {
      used[j] = true;
      final u = g.unitOf[j];
      if (u != seedUnit) _rejected.add(u);
      _recent = _Ring(recentArtists, _recent.items.where((v) => v != u));
    }
  }

  String? flavour() => g.flavour(_recent.items);

  Float32List _reference() {
    final n = g.length;
    // numpy casts a Python float to float32 when it meets a float32 array
    // (NEP 50), but 1 - w is computed in double first.
    final wD = seedFloor + (1.0 - seedFloor) * math.pow(drift, _n);
    final w = _f32(wD);
    final seedRow = g.row(seedUnit);
    final ref = Float32List(n);
    for (var u = 0; u < n; u++) {
      ref[u] = w * seedRow[u];
    }
    final recent = _recent.items;
    if (recent.isNotEmpty) {
      final rows = [for (final u in recent) g.row(u)];
      final rest = _f32(1.0 - wD);
      final mean = Float32List(n);
      for (var u = 0; u < n; u++) {
        var acc = 0.0;
        for (final r in rows) {
          acc = _f32(acc + r[u]);
        }
        mean[u] = acc / rows.length;
        ref[u] = ref[u] + _f32(rest * mean[u]);
      }
    } else {
      for (var u = 0; u < n; u++) {
        ref[u] = ref[u] / w;
      }
    }
    for (var u = 0; u < n; u++) {
      if (_foreign[u]) ref[u] = ref[u] * _crossCulture32;
    }
    if (_rejected.isNotEmpty) {
      final rows = [for (final u in _rejected) g.row(u)];
      for (var u = 0; u < n; u++) {
        var m = rows.first[u];
        for (final r in rows) {
          m = math.max(m, r[u]);
        }
        ref[u] = ref[u] - _f32(repel * m);
      }
    }
    final counts = <int, int>{};
    for (final u in _played.items) {
      counts[u] = (counts[u] ?? 0) + 1;
    }
    counts.forEach((u, k) {
      ref[u] = ref[u] * _f32(math.pow(repeatDecay, k).toDouble());
    });
    return ref;
  }

  Set<String> _keys(int j) => artistKeys?[j] ?? const {};
  String? _album(int j) => albumKeys?[j];

  Float64List _segue() {
    final a = _last;
    final n = _tempo.length;
    final s = Float64List(n);
    final ta = _tempo[a], ea = _energy[a];
    for (var i = 0; i < n; i++) {
      final dt = ((_tempo[i] - ta + 0.5) % 1.0 - 0.5).abs();
      final de = _energy[i] - ea;
      final zt = dt / segueTempo, ze = de / segueEnergy;
      s[i] = math.exp(-0.5 * (zt * zt + ze * ze));
    }
    if (_timed[a] && _allTimed) return s;
    final d = g.dim;
    for (var i = 0; i < n; i++) {
      if (_timed[i] && _timed[a]) continue;
      var dot = 0.0;
      for (var k = 0; k < d; k++) {
        dot += x[i * d + k] * x[a * d + k];
      }
      s[i] = _f32(_f32(dot) + 1.0) / 2.0;
    }
    return s;
  }

  int? _track(int u, Float64List segue) {
    final idx = [
      for (final j in g.tracksOf[u])
        if (!used[j]) j,
    ];
    idx.sort((a, b) => segue[b].compareTo(segue[a]));
    for (final j in idx) {
      if (caps.ok(_keys(j), _album(j))) return j;
    }
    return null;
  }

  int? _pick() {
    final ref = _reference();
    final inside = _inside;
    double floor;
    if (inside != null) {
      for (var u = 0; u < ref.length; u++) {
        if (!inside[u]) ref[u] = double.negativeInfinity;
      }
      floor = double.negativeInfinity;
    } else {
      floor = minAffinity;
    }
    final segue = _segue();
    final order = List.generate(ref.length, (i) => i)
      ..sort((a, b) => ref[b].compareTo(ref[a]));
    int? best;
    var bestScore = double.negativeInfinity;
    for (final u in order) {
      final r = ref[u];
      if (r == double.negativeInfinity ||
          r < floor ||
          _f32(r + _acousticW32) <= bestScore) {
        break;
      }
      final j = _track(u, segue);
      if (j != null && r + acousticW * segue[j] > bestScore) {
        best = j;
        bestScore = r + acousticW * segue[j];
      }
    }
    return best;
  }

  void _emit(int j) {
    final u = g.unitOf[j];
    used[j] = true;
    caps.add(_keys(j), _album(j));
    _last = j;
    _n++;
    _recent.add(u);
    _played.add(u);
  }
}
