import 'dart:math' as math;

import '../core/py_text.dart';
import '../core/py_values.dart';
import 'agent_text.dart';
import 'intent.dart';
import 'runner.dart';

/// `llm_tools.execute_tool` (phase 5, stage d): dispatches one LLM tool call
/// onto [AssistantRunner]'s deterministic handlers, [AssistantLibrary] read
/// queries, and [AssistantPlayer] state.
///
/// Never throws: any error becomes `{"success": false, "error": ...}` so the
/// model can reason about it. Tools whose backend belongs to a later stage or
/// phase (`web_search`, `learn_about`, `find_related_artists` in stage e;
/// `search_online` in phase 6 / decision A; `research_artist_metadata` and
/// `apply_artist_metadata` in phase 7) call the runner's optional hooks when
/// provided, and otherwise return a clear error dict the model can read.

typedef ToolHandler =
    Future<Map<String, Object?>> Function(
      String toolName,
      Map<String, Object?> args,
      AssistantRunner runner,
    );

const _webTools = {'web_search', 'learn_about', 'find_related_artists'};
const _metadataReadTools = {'get_metadata_status', 'get_artist_metadata'};
const _metadataWriteTools = {
  'research_artist_metadata',
  'apply_artist_metadata',
};

const _transportActionMap = {
  'pause': intentPause,
  'resume': intentResume,
  'skip': intentSkip,
  'next': intentSkip,
  'previous': intentPrev,
  'prev': intentPrev,
  'stop': intentStop,
  'clear_queue': intentClearQueue,
  'shuffle': intentShuffle,
};

String _text(Object? v) => v is String ? v : (pyTruthy(v) ? pyStr(v) : '');

int _pyInt(Object? v, int fallback) {
  final raw = pyOr([v, fallback]);
  return switch (raw) {
    int i => i,
    double d => d.truncate(),
    bool b => b ? 1 : 0,
    String s => int.parse(pyStrip(s)),
    _ => throw FormatException('Invalid integer: $raw'),
  };
}

double? _pyFloatOrNull(Object? v) {
  if (v == null) return null;
  return switch (v) {
    int i => i.toDouble(),
    double d => d,
    bool b => b ? 1.0 : 0.0,
    String s => double.parse(pyStrip(s)),
    _ => throw FormatException('Invalid float: $v'),
  };
}

/// `_num`: a stored number, or [fallback] when missing/unparseable. Keeps 0.0.
double _num(Object? v, double fallback) {
  if (v == null || v == '') return fallback;
  return switch (v) {
    int i => i.toDouble(),
    double d => d,
    bool b => b ? 1.0 : 0.0,
    String s => double.tryParse(pyStrip(s)) ?? fallback,
    _ => fallback,
  };
}

List<T> _stableSorted<T>(List<T> xs, int Function(T, T) compare) {
  final idx = List<int>.generate(xs.length, (i) => i)
    ..sort((i, j) {
      final c = compare(xs[i], xs[j]);
      return c != 0 ? c : i - j;
    });
  return [for (final i in idx) xs[i]];
}

/// `llm_tools.execute_tool`.
Future<Map<String, Object?>> executeTool(
  String toolName,
  Map<String, Object?> args,
  AssistantRunner runner,
) async {
  runner.recordAgentToolUsed(toolName);

  try {
    // ── Read-only library search ───────────────────────────────────────────
    if (toolName == 'search_library') {
      final query = pyStrip(_text(args['query']));
      final genre = pyLower(pyStrip(_text(args['genre'])));
      final limit = _pyInt(args['limit'], 10);

      final List<Row> rows;
      if (query.isNotEmpty && genre.isEmpty) {
        rows = await runner.library.searchTracksSimple(
          query,
          limit: math.max(limit, 10),
        );
      } else {
        rows = await runner.library.getAllTracks(searchQuery: query);
      }

      final out = <Map<String, Object?>>[];
      for (final d in rows) {
        final g = _text(d['genre']);
        if (genre.isNotEmpty && !pyLower(g).contains(genre)) continue;
        out.add({
          'path': d['path'],
          'title': pyOr([d['title'], d['track_title'], 'Unknown']),
          'artist': pyOr([d['artist'], d['artist_name'], 'Unknown Artist']),
          'album': pyOr([d['album'], d['album_title'], '']),
          'genre': g,
        });
        if (out.length >= limit) break;
      }
      return {'success': true, 'count': out.length, 'tracks': out};
    }

    // ── Playback: play (destructive -> gated when queue non-empty) ─────────
    if (toolName == 'play_music') {
      final query = pyStrip(_text(args['query']));
      final path = pyStrip(_text(args['path']));
      final extras = await _resolvedExtras(runner, path);
      if (query.isEmpty && extras.isEmpty) {
        return {
          'success': false,
          'error': 'Nothing to play — provide a query or path.',
        };
      }

      final label = _playLabel(extras, query);
      if (runner.player.queue.isNotEmpty) {
        return runner.agentRequestConfirmation(
          spoken:
              'That will replace your current queue with $label, sir. '
              'Shall I proceed?',
          displayed:
              'This will **replace your current queue** with $label. Proceed?',
          onYes: () => runner.executeIntent(
            Intent(
              intentPlayNow,
              query: query,
              raw: query,
              extras: Map.of(extras),
            ),
          ),
        );
      }
      return await runner.agentRunIntent(
        intentPlayNow,
        query: query,
        extras: extras,
      );
    }

    if (toolName == 'enqueue_music') {
      final query = pyStrip(_text(args['query']));
      final extras = await _resolvedExtras(
        runner,
        pyStrip(_text(args['path'])),
      );
      return await runner.agentRunIntent(
        intentQueueAdd,
        query: query,
        extras: extras,
      );
    }

    if (toolName == 'play_next') {
      final query = pyStrip(_text(args['query']));
      final extras = await _resolvedExtras(
        runner,
        pyStrip(_text(args['path'])),
      );
      return await runner.agentRunIntent(
        intentQueueNext,
        query: query,
        extras: extras,
      );
    }

    if (toolName == 'play_similar') {
      return await runner.agentRunIntent(intentPlaySimilar);
    }

    if (toolName == 'steer_mood') {
      final mood = pyStrip(_text(pyOr([args['mood'], 'chill'])));
      return await runner.agentRunIntent(intentMoodSteer, query: mood);
    }

    if (toolName == 'search_by_acoustic_profile') {
      final minE = _pyFloatOrNull(args['min_energy']);
      final maxE = _pyFloatOrNull(args['max_energy']);
      final minB = _pyFloatOrNull(args['min_brightness']);
      final maxB = _pyFloatOrNull(args['max_brightness']);
      final minBpm = _pyFloatOrNull(args['min_bpm']);
      final maxBpm = _pyFloatOrNull(args['max_bpm']);
      final genre = pyLower(pyStrip(_text(args['genre'])));
      final limit = _pyInt(args['limit'], 10);

      final allTracks = await runner.library.getAllTracks();
      final matches = <Map<String, Object?>>[];
      for (final d in allTracks) {
        final g = pyLower(_text(d['genre']));
        if (genre.isNotEmpty && !g.contains(genre)) continue;
        final bpm = _num(d['bpm'] ?? d['tempo'], 0.0);
        if (minBpm != null && bpm > 0 && bpm < minBpm) continue;
        if (maxBpm != null && bpm > 0 && bpm > maxBpm) continue;
        final energy = _num(d['energy'], 0.5);
        if (minE != null && energy < minE) continue;
        if (maxE != null && energy > maxE) continue;
        final bright = _num(d['brightness'], 0.5);
        if (minB != null && bright < minB) continue;
        if (maxB != null && bright > maxB) continue;
        matches.add(slimTrack(d));
        if (matches.length >= limit) break;
      }
      return {'success': true, 'count': matches.length, 'tracks': matches};
    }

    if (toolName == 'playback_control') {
      final action = pyLower(pyStrip(_text(args['action'])));
      final intentName = _transportActionMap[action];
      if (intentName == null) {
        return {'success': false, 'error': 'Unknown transport action: $action'};
      }
      return await runner.agentRunIntent(intentName);
    }

    if (toolName == 'save_queue_as_playlist') {
      final name = pyStrip(_text(pyOr([args['name'], 'Saved Walk'])));
      return await runner.agentRunIntent(intentSaveQueue, query: name);
    }

    if (toolName == 'get_current_track_info') {
      return await runner.agentRunIntent(intentTrackInfo);
    }

    // ── Read-only player status ────────────────────────────────────────────
    if (toolName == 'get_player_status') {
      final queue = runner.player.queue;
      final idx = runner.player.currentIndex;
      Map<String, Object?>? current;
      if (idx >= 0 && idx < queue.length) {
        final c = queue[idx];
        current = {
          'title': pyOr([
            c['track_title'],
            c.containsKey('title') ? c['title'] : 'Unknown',
          ]),
          'artist': pyOr([
            c['artist_name'],
            c.containsKey('artist') ? c['artist'] : 'Unknown Artist',
          ]),
          'album': pyOr([
            c['album_title'],
            c.containsKey('album') ? c['album'] : '',
          ]),
        };
      }
      return {
        'success': true,
        'is_playing': runner.player.isPlaying,
        'currently_playing': current,
        'queue_length': queue.length,
      };
    }

    // ── Online search (phase 6 / decision A) ───────────────────────────────
    if (toolName == 'search_online') {
      final hook = runner.onlineSearchHandler;
      if (hook != null) return await hook(toolName, args, runner);
      return {'success': false, 'error': 'Online search is not available yet.'};
    }

    // ── Library knowledge (read-only) ──────────────────────────────────────
    if (toolName == 'get_library_overview') {
      final total = await runner.library.getTotalTracks();
      final artists = await runner.library.getAllArtists();
      final albums = await runner.library.getAllAlbums();
      final playlists = await runner.library.getAllPlaylists();
      final genreCounts = <String, int>{};
      for (final row in await runner.library.getAllTracks()) {
        final g = pyStrip(_text(row['genre']));
        if (g.isNotEmpty) {
          genreCounts[g] = (genreCounts[g] ?? 0) + 1;
        }
      }
      final top = _stableSorted(
        genreCounts.entries.toList(),
        (a, b) => b.value.compareTo(a.value),
      ).take(8);
      return {
        'success': true,
        'total_tracks': total,
        'artists': artists.length,
        'albums': albums.length,
        'playlists': playlists.length,
        'top_genres': [
          for (final e in top) {'genre': e.key, 'count': e.value},
        ],
      };
    }

    if (toolName == 'get_top_played') {
      final rows = await runner.library.getMostPlayed(
        limit: _pyInt(args['limit'], 10),
      );
      return {
        'success': true,
        'tracks': [for (final r in rows) slimTrack(r)],
      };
    }

    if (toolName == 'list_artists') {
      final search = pyStrip(_text(args['search']));
      final limit = _pyInt(args['limit'], 25);
      final rows = await runner.library.getAllArtists(searchQuery: search);
      final slice = _slice(rows, limit);
      final out = [
        for (final r in slice)
          {
            'name': r['name'],
            'track_count': r['track_count'],
            'album_count': r['album_count'],
          },
      ];
      return {'success': true, 'count': out.length, 'artists': out};
    }

    if (toolName == 'get_artist_albums') {
      final artist = pyStrip(_text(args['artist']));
      if (artist.isEmpty) {
        return {'success': false, 'error': 'No artist given.'};
      }
      final rows = await runner.library.getAlbumsByArtist(artist);
      final out = [
        for (final r in rows)
          {
            'album': r['album'],
            'year': r['year'],
            'genre': r['genre'],
            'track_count': r['track_count'],
          },
      ];
      return {
        'success': true,
        'artist': artist,
        'count': out.length,
        'albums': out,
      };
    }

    if (toolName == 'get_album_tracks') {
      final tracks = await _resolveAlbumTracks(
        runner,
        pyStrip(_text(args['album'])),
        pyStrip(_text(args['artist'])),
      );
      return {
        'success': true,
        'count': tracks.length,
        'tracks': [for (final t in tracks) slimTrack(t)],
      };
    }

    if (toolName == 'list_playlists') {
      final rows = await runner.library.getAllPlaylists();
      final out = [
        for (final r in rows)
          {'name': r['name'], 'track_count': r['track_count']},
      ];
      return {'success': true, 'count': out.length, 'playlists': out};
    }

    if (toolName == 'get_playlist_tracks') {
      final (pid, pname) = await _resolvePlaylist(
        runner,
        pyStrip(_text(args['name'])),
      );
      if (pid == null) {
        return {
          'success': false,
          'message': "No playlist matching '${pyStr(args['name'])}'.",
        };
      }
      final rows = await runner.library.getTracksInPlaylist(pid);
      return {
        'success': true,
        'playlist': pname,
        'count': rows.length,
        'tracks': [for (final r in rows) slimTrack(r)],
      };
    }

    if (toolName == 'get_recently_played') {
      final rows = await runner.library.getRecentTracks(
        limit: _pyInt(args['limit'], 15),
      );
      return {
        'success': true,
        'tracks': [for (final r in rows) slimTrack(r)],
      };
    }

    if (toolName == 'get_track_details') {
      var path = pyStrip(_text(args['path']));
      final query = pyStrip(_text(args['query']));
      if (path.isEmpty && query.isEmpty) {
        path = runner.player.currentPath;
      }
      if (path.isEmpty && query.isNotEmpty) {
        final hit = await runner.library.searchTracksSimple(query, limit: 1);
        if (hit.isNotEmpty) {
          path = _text(hit.first['path']);
        }
      }
      if (path.isEmpty) {
        return {'success': false, 'message': 'No matching track.'};
      }
      final full = await runner.library.getTrackFull(path);
      if (full == null) {
        return {'success': false, 'message': 'Track not found.'};
      }
      return {'success': true, 'track': slimTrack(full)};
    }

    // ── Album / playlist playback (destructive replace -> gated when busy) ─
    if (toolName == 'play_album') {
      final album = pyStrip(_text(args['album']));
      final tracks = await _resolveAlbumTracks(
        runner,
        album,
        pyStrip(_text(args['artist'])),
      );
      if (tracks.isEmpty) {
        return {'success': false, 'message': "No album matching '$album'."};
      }
      return await _playOrGate(runner, tracks, "the album '$album'");
    }

    if (toolName == 'play_playlist') {
      final (pid, pname) = await _resolvePlaylist(
        runner,
        pyStrip(_text(args['name'])),
      );
      if (pid == null) {
        return {
          'success': false,
          'message': "No playlist matching '${pyStr(args['name'])}'.",
        };
      }
      final tracks = await runner.library.getTracksInPlaylist(pid);
      if (tracks.isEmpty) {
        return {'success': false, 'message': "Playlist '$pname' is empty."};
      }
      return await _playOrGate(runner, tracks, "the playlist '$pname'");
    }

    // ── Metadata tools (read-only here; research/apply in phase 7) ─────────
    if (_metadataReadTools.contains(toolName)) {
      return await _metadataReadTool(toolName, args, runner);
    }

    if (_metadataWriteTools.contains(toolName)) {
      final hook = runner.metadataWriteHandler;
      if (hook != null) return await hook(toolName, args, runner);
      return {
        'success': false,
        'error': 'Metadata curation is not available yet.',
      };
    }

    // ── Web tools (stage e) ────────────────────────────────────────────────
    if (_webTools.contains(toolName)) {
      final hook = runner.webToolHandler;
      if (hook != null) return await hook(toolName, args, runner);
      return {'success': false, 'error': 'Web research is not available yet.'};
    }

    return {'success': false, 'error': 'Unknown tool: $toolName'};
  } catch (e) {
    return {'success': false, 'error': '$e'};
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

List<T> _slice<T>(List<T> xs, int stop) {
  final end = stop < 0
      ? math.max(0, xs.length + stop)
      : math.min(xs.length, stop);
  return xs.sublist(0, end);
}

Future<Map<String, Object?>> _resolvedExtras(
  AssistantRunner runner,
  String path,
) async {
  if (path.isEmpty) return {};
  try {
    final track = await runner.library.getTrackFull(path);
    if (track != null) {
      return {'resolved_track': slimTrack(track)};
    }
  } catch (_) {}
  return {};
}

String _playLabel(Map<String, Object?> extras, String query) {
  final t = extras['resolved_track'];
  if (t is Map<String, Object?> && t.isNotEmpty) {
    final title = pyStr(pyOr([t['title'], t['track_title']]));
    final artist = pyStr(pyOr([t['artist'], t['artist_name']]));
    return "'$title' by $artist";
  }
  return "'$query'";
}

Future<List<Row>> _resolveAlbumTracks(
  AssistantRunner runner,
  String album,
  String artist,
) async {
  if (album.isEmpty) return const [];
  final albums = await runner.library.getAllAlbums(searchQuery: album);
  final alLow = pyLower(pyStrip(album));
  final arLow = pyLower(pyStrip(artist));
  Row? chosen;
  for (final ad in albums) {
    if (pyLower(pyStrip(_text(ad['album']))) == alLow) {
      if (arLow.isEmpty || pyLower(pyStrip(_text(ad['artist']))) == arLow) {
        chosen = ad;
        break;
      }
    }
  }
  if (chosen == null && albums.isNotEmpty) {
    chosen = albums.first;
  }
  if (chosen == null) return const [];
  return await runner.library.getTracksByAlbum(
    _text(chosen['album']),
    _text(chosen['artist']),
  );
}

Future<(int?, String?)> _resolvePlaylist(
  AssistantRunner runner,
  String name,
) async {
  if (name.isEmpty) return (null, null);
  final rows = await runner.library.getAllPlaylists(searchQuery: name);
  final low = pyLower(pyStrip(name));
  for (final rd in rows) {
    if (pyLower(pyStrip(_text(rd['name']))) == low) {
      return (rd['id'] as int?, rd['name'] as String?);
    }
  }
  if (rows.isNotEmpty) {
    final rd = rows.first;
    return (rd['id'] as int?, rd['name'] as String?);
  }
  return (null, null);
}

Future<Map<String, Object?>> _playOrGate(
  AssistantRunner runner,
  List<Row> tracks,
  String label,
) async {
  if (runner.player.queue.isNotEmpty) {
    return runner.agentRequestConfirmation(
      spoken:
          'That will replace your current queue with $label, sir. '
          'Shall I proceed?',
      displayed:
          'This will **replace your current queue** with $label. Proceed?',
      onYes: () async => runner.stageTracks(tracks, label),
    );
  }
  return await runner.agentPlayTracks(tracks, label);
}

Future<String?> _resolveArtist(AssistantRunner runner, String name) async {
  final clean = pyStrip(name);
  if (clean.isEmpty) return null;
  final rows = await runner.library.getAllArtists(searchQuery: clean);
  final low = pyLower(clean);
  for (final r in rows) {
    if (pyLower(_text(r['name'])) == low) return r['name'] as String?;
  }
  return rows.isNotEmpty ? rows.first['name'] as String? : null;
}

List<Object?> _genreList(Object? items) {
  if (items is! List) return const [];
  return [
    for (final g in items)
      if (pyTruthy(g)) g is Map<String, Object?> ? g['name'] : pyStr(g),
  ];
}

Future<Map<String, Object?>> _metadataReadTool(
  String toolName,
  Map<String, Object?> args,
  AssistantRunner runner,
) async {
  if (toolName == 'get_metadata_status') {
    final gaps = await runner.library.getMetadataGapArtists(limit: 2000);
    final cov = await runner.library.getMetadataCoverage(gaps: gaps);
    final pending = await runner.library.getPendingAiResearch();
    final blocking = [
      for (final g in gaps)
        if (g['gap_severity'] == 0) g['artist_name'],
    ];
    final pendingSorted = pending.keys.toList()..sort(pyStrCompare);
    return {
      'success': true,
      'artists': cov['artists'] ?? 0,
      'placeable': cov['placeable'] ?? 0,
      'missing_genre_and_country': blocking.take(25).toList(),
      'missing_count': blocking.length,
      'ai_suggestions_pending': pendingSorted.take(25).toList(),
      'research_available': runner.researchAvailable(),
      'where_to_review': 'Settings → Metadata',
    };
  }

  final artist = await _resolveArtist(runner, _text(args['artist']));
  if (artist == null) {
    return {
      'success': false,
      'message': "No library artist matching '${pyStr(args['artist'])}'.",
    };
  }

  final enr = await runner.library.getArtistEnrichment(artist) ?? const {};
  final cached = await runner.library.getAiResearch(artist);
  final out = <String, Object?>{
    'success': true,
    'artist': artist,
    'genres': _genreList(enr['genres']),
    'country': pyOr([enr['country'], '']),
    'provenance': pyOr([enr['provenance'], <String, Object?>{}]),
    'file_tags': await runner.library.getArtistSourceGenres(artist),
  };
  if (cached != null) {
    final p = cached['payload']! as Map<String, Object?>;
    out['ai_suggestion'] = {
      'state': cached['state'],
      'genres': p['genres'],
      'country': p['country'],
      'summary': p['summary'],
      'confidence': p['confidence'],
    };
  }
  return out;
}
