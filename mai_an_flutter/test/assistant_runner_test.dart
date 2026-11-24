import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/runner.dart';
import 'package:mai_an_lab/assistant/runner_phrases.dart';
import 'package:mai_an_lab/core/py_difflib.dart';
import 'package:mai_an_lab/core/py_json.dart';
import 'package:mai_an_lab/core/py_text.dart';

/// Phase 5 stage c1 against Python's own `AssistantRunner` outputs
/// (`tool/export_runner_goldens.py`).
void main() {
  final golden =
      jsonDecode(File('test/goldens/assistant_runner.json').readAsStringSync())
          as Map<String, Object?>;

  test('kJarvisPhrases matches Python JARVIS_PHRASES', () {
    expect(
      pyJsonDumps(_sortKeys(kJarvisPhrases), ensureAscii: false),
      pyJsonDumps(_sortKeys(golden['phrases']), ensureAscii: false),
    );
  });

  test('pySequenceRatio matches difflib.SequenceMatcher.ratio', () {
    final cases = (golden['difflib']! as List<Object?>).cast<List<Object?>>();
    expect(cases.length, greaterThanOrEqualTo(40));
    for (final c in cases) {
      final a = c[0]! as String;
      final b = c[1]! as String;
      final want = (c[2]! as num).toDouble();
      expect(
        pySequenceRatio(a, b),
        want,
        reason: 'a=${jsonEncode(a)} b=${jsonEncode(b)}',
      );
    }
  });

  test('pyIsDigit matches str.isdigit across all Unicode code points', () {
    final want = (golden['isdigit_hashes']! as List<Object?>).cast<int>();
    final got = <int>[];
    var h = 0x811c9dc5;
    for (var o = 0; o <= 0x10ffff; o++) {
      if (o < 0xd800 || o > 0xdfff) {
        final b = pyIsDigit(String.fromCharCode(o)) ? 1 : 0;
        h = ((h ^ b) * 0x01000193) & 0xffffffff;
      }
      if (o % 4096 == 4095) {
        got.add(h);
        h = 0x811c9dc5;
      }
    }
    expect(got, want);
  });

  test('pyCapitalize matches str.capitalize', () {
    for (final c
        in (golden['capitalize']! as List<Object?>).cast<List<Object?>>()) {
      expect(pyCapitalize(c[0]! as String), c[1], reason: jsonEncode(c[0]));
    }
  });

  group('AssistantRunner scenarios match Python turn by turn', () {
    final scenarios = (golden['scenarios']! as List<Object?>)
        .cast<Map<String, Object?>>();
    for (final sc in scenarios) {
      test(sc['name']! as String, () async {
        final calls = <Object?>[];
        final lib = _FakeLibrary(sc['library']! as Map<String, Object?>, calls);
        final eng = _FakeEngine(sc['engine']! as Map<String, Object?>, calls);
        final baseClock = (sc['clock']! as List<Object?>).cast<int>();
        var activeClock = baseClock;

        final runner = AssistantRunner(
          library: lib,
          player: eng,
          random: _ScriptedRandom(sc['seed']! as int),
          now: () => DateTime.utc(
            activeClock[0],
            activeClock[1],
            activeClock[2],
            activeClock[3],
            activeClock[4],
            activeClock[5],
          ),
        );

        final turns = (sc['turns']! as List<Object?>)
            .cast<Map<String, Object?>>();
        final expected = (sc['expected']! as List<Object?>)
            .cast<Map<String, Object?>>();
        expect(turns.length, expected.length);

        for (var i = 0; i < turns.length; i++) {
          calls.clear();
          final turn = turns[i];
          activeClock = turn.containsKey('clock')
              ? (turn['clock']! as List<Object?>).cast<int>()
              : baseClock;
          final hist = (turn['history'] as List<Object?>?) ?? const [];
          final resp = await runner.dispatchText(
            turn['text']! as String,
            historyProvider: () => hist,
          );
          final intent = resp.intent;
          final got = {
            'spoken': resp.spoken,
            'displayed': resp.displayed,
            'success': resp.success,
            'extras': resp.extras,
            'action': resp.action,
            'deferred_play': resp.deferredPlay,
            'intent': intent == null
                ? null
                : {
                    'name': intent.name,
                    'query': intent.query,
                    'raw': intent.raw,
                    'extras': intent.extras,
                  },
            'calls': List<Object?>.of(calls),
            'pending': _serializePending(runner.pending),
            'choice': _serializeChoice(runner.pendingChoice),
            'recent': runner.recent,
            'queue': [for (final t in eng.queue) t['path']],
            'current_index': eng.currentIndex,
            'is_playing': eng.isPlaying,
            'is_shuffle': eng.isShuffle,
          };

          expect(
            pyJsonDumps(_sortKeys(got), ensureAscii: false),
            pyJsonDumps(_sortKeys(expected[i]), ensureAscii: false),
            reason:
                'scenario ${sc['name']} turn $i (${jsonEncode(turn['text'])})',
          );
        }
      });
    }
  });
}

Object? _sortKeys(Object? v) => switch (v) {
  Map<Object?, Object?> m => {
    for (final k in m.keys.map((e) => e.toString()).toList()..sort())
      k: _sortKeys(m[k]),
  },
  List<Object?> l => [for (final e in l) _sortKeys(e)],
  _ => v,
};

Map<String, Object?>? _serializePending(PendingConfirmation? p) => p == null
    ? null
    : {
        'prompt': p.prompt,
        'on_yes_action': p.onYesAction,
        'on_yes_msg': p.onYesMsg,
        'on_no_msg': p.onNoMsg,
        'has_on_yes': p.onYes != null,
        'has_on_no': p.onNo != null,
      };

Map<String, Object?>? _serializeChoice(PendingChoice? c) => c == null
    ? null
    : {
        'prompt': c.prompt,
        'options': [
          for (final o in c.options)
            {'id': o.id, 'title': o.title, 'subtitle': o.subtitle},
        ],
      };

Row _deepCopyRow(Map<String, Object?> m) => {
  for (final e in m.entries)
    e.key: e.value is Map<String, Object?>
        ? _deepCopyRow(e.value! as Map<String, Object?>)
        : e.value is List<Object?>
        ? List<Object?>.of(e.value! as List<Object?>)
        : e.value,
};

class _ScriptedRandom implements AssistantRandom {
  _ScriptedRandom(int seed) : _state = BigInt.from(seed) & _mask64;

  static final _mul = BigInt.parse('6364136223846793005');
  static final _inc = BigInt.parse('1442695040888963407');
  static final _mask64 = (BigInt.one << 64) - BigInt.one;
  BigInt _state;

  int _next() {
    _state = (_state * _mul + _inc) & _mask64;
    return (_state >> 32).toInt();
  }

  @override
  int randbelow(int n) => _next() % n;

  @override
  double random() => _next() / 4294967296.0;
}

class _FakeLibrary implements AssistantLibrary {
  _FakeLibrary(Map<String, Object?> spec, this._calls)
    : _tracks = [
        for (final t in (spec['tracks'] as List<Object?>?) ?? const [])
          _deepCopyRow(t! as Map<String, Object?>),
      ],
      _mostPlayed = [
        for (final t in (spec['most_played'] as List<Object?>?) ?? const [])
          _deepCopyRow(t! as Map<String, Object?>),
      ],
      _analysed = {
        for (final p in (spec['analysed'] as List<Object?>?) ?? const [])
          p! as String,
      },
      _recentPlayed = {
        for (final p in (spec['recent_played'] as List<Object?>?) ?? const [])
          p! as String,
      },
      _walks = {
        for (final e
            in ((spec['walks'] as Map<String, Object?>?) ?? const {}).entries)
          e.key: (e.value! as List<Object?>).cast<String>().toList(),
      },
      _neighbors = {
        for (final e
            in ((spec['neighbors'] as Map<String, Object?>?) ?? const {})
                .entries)
          e.key: (e.value! as List<Object?>).cast<String>().toList(),
      },
      _playlists = {
        for (final e
            in ((spec['playlists'] as Map<String, Object?>?) ?? const {})
                .entries)
          e.key: e.value! as int,
      } {
    _nextPid = _playlists.isEmpty ? 1 : _playlists.values.reduce(math.max) + 1;
  }

  final List<Row> _tracks;
  final List<Row> _mostPlayed;
  final Set<String> _analysed;
  final Set<String> _recentPlayed;
  final Map<String, List<String>> _walks;
  final Map<String, List<String>> _neighbors;
  final Map<String, int> _playlists;
  late int _nextPid;
  final List<Object?> _calls;

  Row _hydrate(Row t, {bool includeTimbre = false}) {
    final row = _deepCopyRow(t);
    final hasTimbre = row.remove('_timbre') == true;
    if (includeTimbre && hasTimbre) {
      row['timbre'] = Uint8List.fromList([0, 1, 2, 3]);
    }
    return row;
  }

  @override
  Future<List<Row>> searchTracksSimple(
    String query, {
    required int limit,
  }) async {
    final q = pyLower(query);
    final out = <Row>[];
    for (final t in _tracks) {
      final hay = pyLower(
        '${t['title'] ?? ''} ${t['artist'] ?? ''} ${t['album'] ?? ''}',
      );
      if (hay.contains(q)) {
        out.add(_hydrate(t));
        if (out.length >= limit) break;
      }
    }
    return out;
  }

  @override
  Future<List<Row>> getAllTracks() async => [
    for (final t in _tracks) _hydrate(t),
  ];

  @override
  Future<Row?> getTrackFull(String path) async {
    for (final t in _tracks) {
      if (t['path'] == path) return _hydrate(t, includeTimbre: true);
    }
    return null;
  }

  @override
  Future<bool> hasCurrentFeatures(String path) async =>
      _analysed.contains(path);

  @override
  Future<Set<String>> recentPlayedPaths({required int windowSeconds}) async =>
      Set<String>.of(_recentPlayed);

  @override
  Future<void> recordPlayback(
    String path,
    String event, {
    String? seedPath,
  }) async {
    _calls.add(['record_playback', path, event, seedPath]);
    _recentPlayed.add(path);
  }

  @override
  Future<List<Row>> getMostPlayed({required int limit}) async => [
    for (final t in _mostPlayed.take(limit)) _hydrate(t),
  ];

  @override
  Future<List<String>> searchArtistNames(String query) async {
    final q = pyLower(query);
    final seen = <String>[];
    for (final t in _tracks) {
      final a = (t['artist'] as String?) ?? '';
      if (a.isNotEmpty && pyLower(a).contains(q) && !seen.contains(a)) {
        seen.add(a);
      }
    }
    return seen;
  }

  @override
  Future<String?> firstTrackPathByArtist(String name) async {
    final target = pyLower(name);
    for (final t in _tracks) {
      final a = (t['artist'] as String?) ?? '';
      if (pyLower(a) == target) return t['path'] as String?;
    }
    return null;
  }

  @override
  Future<int> createPlaylist(String name) async {
    final lower = pyLower(name);
    if (_playlists.keys.any((k) => pyLower(k) == lower)) {
      throw StateError('UNIQUE constraint failed: playlists.name');
    }
    final pid = _nextPid++;
    _playlists[name] = pid;
    _calls.add(['create_playlist', name, pid]);
    return pid;
  }

  @override
  Future<void> addTrackToPlaylist(int playlistId, String path) async {
    _calls.add(['add_track_to_playlist', playlistId, path]);
  }

  @override
  Future<List<String>> walk(
    String seedPath, {
    required int length,
    required Set<String> avoid,
  }) async {
    final sortedAvoid = avoid.toList()..sort(pyStrCompare);
    _calls.add(['walk', seedPath, length, sortedAvoid]);
    return [
      for (final p in _walks[seedPath] ?? const <String>[])
        if (!avoid.contains(p)) p,
    ].take(length).toList();
  }

  @override
  Future<List<String>> artistNeighbors(
    String seedPath, {
    required int k,
  }) async {
    _calls.add(['neighbors', seedPath, k, 'artist']);
    return (_neighbors[seedPath] ?? const <String>[]).take(k).toList();
  }
}

class _FakeEngine implements AssistantPlayer {
  _FakeEngine(Map<String, Object?> spec, this._calls)
    : queue = [
        for (final t in (spec['queue'] as List<Object?>?) ?? const [])
          _deepCopyRow(t! as Map<String, Object?>),
      ],
      currentIndex = (spec['current_index'] as int?) ?? 0,
      currentPath = (spec['current_path'] as String?) ?? '',
      currentTrack = (spec['current_track'] as String?) ?? '',
      currentArtist = (spec['current_artist'] as String?) ?? '',
      currentAlbum = (spec['current_album'] as String?) ?? '',
      duration = ((spec['duration'] as num?) ?? 0.0).toDouble(),
      isPlaying = (spec['is_playing'] as bool?) ?? false,
      _isShuffle = (spec['is_shuffle'] as bool?) ?? false;

  @override
  List<Row> queue;
  @override
  int currentIndex;
  @override
  String currentPath;
  @override
  String currentTrack;
  @override
  String currentArtist;
  @override
  String currentAlbum;
  @override
  double duration;
  @override
  bool isPlaying;

  bool _isShuffle;
  final List<Object?> _calls;

  @override
  bool get isShuffle => _isShuffle;

  @override
  set isShuffle(bool value) {
    _isShuffle = value;
    _calls.add(['is_shuffle', value]);
  }

  void _syncCurrent() {
    if (currentIndex >= 0 && currentIndex < queue.length) {
      final t = queue[currentIndex];
      currentPath = (t['path'] as String?) ?? '';
      currentTrack =
          (t['track_title'] as String?) ?? (t['title'] as String?) ?? '';
      currentArtist =
          (t['artist_name'] as String?) ?? (t['artist'] as String?) ?? '';
      currentAlbum =
          (t['album_title'] as String?) ?? (t['album'] as String?) ?? '';
      duration = ((t['duration'] as num?) ?? 0.0).toDouble();
    } else {
      currentPath = '';
      currentTrack = '';
      currentArtist = '';
      currentAlbum = '';
      duration = 0.0;
    }
  }

  @override
  void setQueue(List<Row> tracks, {int startIndex = 0}) {
    _calls.add([
      'set_queue',
      [for (final t in tracks) t['path']],
      startIndex,
    ]);
    queue = [for (final t in tracks) _deepCopyRow(t)];
    currentIndex = queue.isEmpty
        ? 0
        : math.min(math.max(0, startIndex), queue.length - 1);
    _syncCurrent();
  }

  @override
  void queueLast(Row track) {
    _calls.add(['queue_last', track['path']]);
    queue.add(_deepCopyRow(track));
  }

  @override
  void queueNext(Row track) {
    _calls.add(['queue_next', track['path']]);
    final at = math.min(currentIndex + 1, queue.length);
    queue.insert(at, _deepCopyRow(track));
  }

  @override
  void queueLastMany(List<Row> tracks) {
    _calls.add([
      'queue_last_many',
      [for (final t in tracks) t['path']],
    ]);
    queue.addAll([for (final t in tracks) _deepCopyRow(t)]);
  }

  @override
  void removeFromQueue(int index) {
    _calls.add(['remove_from_queue', index]);
    if (index < 0 || index >= queue.length) return;
    final removedActive = index == currentIndex;
    queue.removeAt(index);
    if (queue.isEmpty) {
      isPlaying = false;
      currentIndex = 0;
      _syncCurrent();
      return;
    }
    if (index < currentIndex) {
      currentIndex--;
    } else if (removedActive) {
      currentIndex = math.min(currentIndex, queue.length - 1);
      _syncCurrent();
    }
  }

  @override
  void moveQueueItem(int oldIndex, int newIndex) {
    _calls.add(['move_queue_item', oldIndex, newIndex]);
    if (oldIndex < 0 ||
        oldIndex >= queue.length ||
        newIndex < 0 ||
        newIndex >= queue.length) {
      return;
    }
    final item = queue.removeAt(oldIndex);
    queue.insert(newIndex, item);
    final cur = currentIndex;
    if (oldIndex == cur) {
      currentIndex = newIndex;
    } else if (oldIndex < cur && cur <= newIndex) {
      currentIndex = cur - 1;
    } else if (newIndex <= cur && cur < oldIndex) {
      currentIndex = cur + 1;
    }
  }

  @override
  void next() => _calls.add(['next']);

  @override
  void previous() => _calls.add(['previous']);

  @override
  void pause() {
    _calls.add(['pause']);
    isPlaying = false;
  }

  @override
  void play() {
    _calls.add(['play']);
    isPlaying = true;
  }

  @override
  void stop() {
    _calls.add(['stop']);
    isPlaying = false;
    currentPath = '';
    currentTrack = '';
    currentArtist = '';
    currentAlbum = '';
    duration = 0.0;
  }

  @override
  void clearQueue() {
    _calls.add(['clear_queue']);
    isPlaying = false;
    queue.clear();
    currentIndex = 0;
    currentPath = '';
    currentTrack = '';
    currentArtist = '';
    currentAlbum = '';
    duration = 0.0;
  }
}
