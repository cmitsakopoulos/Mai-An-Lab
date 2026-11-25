import 'dart:async';

import '../assistant/web_research.dart' show ResearchUnavailable;
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../data/library_repository.dart';
import '../metadata/metadata_ai.dart';
import 'runner.dart';
import 'tool_executor.dart';

/// `llm_tools.py`'s metadata write tools for [AssistantRunner]:
/// `research_artist_metadata` (an AI suggestion, never applied) and
/// `apply_artist_metadata` (genres/country on one or many artists, gated on
/// a yes/no confirmation).
ToolHandler buildMetadataWriteHandler({
  required LibraryRepository? Function() repository,
  required MetadataResearcher Function(LibraryRepository repo) researcher,
  required Future<Object?> Function() refreshWalkModels,
}) {
  return (toolName, args, runner) async {
    final repo = repository();
    if (repo == null || !repo.writable) {
      return {'success': false, 'error': 'Library unavailable.'};
    }
    if (toolName == 'apply_artist_metadata') {
      return _apply(args, runner, repo, researcher(repo), refreshWalkModels);
    }
    final artist = await _resolveArtist(runner, _text(args['artist']));
    if (artist == null) {
      return {
        'success': false,
        'message': "No library artist matching '${pyStr(args['artist'])}'.",
      };
    }
    if (toolName == 'research_artist_metadata') {
      final Map<String, Object?> res;
      try {
        res = await researcher(
          repo,
        ).researchArtist(artist, force: pyTruthy(args['refresh']));
      } on ResearchUnavailable catch (e) {
        return {'success': false, 'error': e.message};
      }
      return {
        'success': true,
        'artist': artist,
        'status': res['status'],
        'summary': res['summary'],
        'genres': res['genres'],
        'country': res['country'],
        'confidence': res['confidence'],
        'sources': [
          for (final s in (res['sources'] as List?) ?? const [])
            (s as Map)['title'],
        ],
        'saved_as':
            'suggestion — not applied; the user can accept it in Settings → '
            'Metadata or ask you to apply it',
      };
    }
    return {'success': false, 'error': 'Unknown tool: $toolName'};
  };
}

String _text(Object? v) => v is String ? v : (v == null ? '' : pyStr(v));

/// `_resolve_artist`: the exact (case-insensitive) library name first, else
/// the first fuzzy hit.
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

/// Python's `repr()` of a short string.
String _repr(String s) => s.contains("'") && !s.contains('"')
    ? '"$s"'
    : "'${s.replaceAll("'", r"\'")}'";

/// `_apply_metadata`: REPLACES genres by default (mode "add" merges); with
/// no genres or country, a single artist takes its pending AI suggestion.
Future<Map<String, Object?>> _apply(
  Map<String, Object?> args,
  AssistantRunner runner,
  LibraryRepository repo,
  MetadataResearcher researcher,
  Future<Object?> Function() refresh,
) async {
  var requested = [
    for (final a in (args['artists'] as List?) ?? const [])
      if (pyStrip(_text(a)).isNotEmpty) _text(a),
  ];
  if (requested.isEmpty && pyTruthy(args['artist'])) {
    requested = [_text(args['artist'])];
  }
  final artists = <String>[], missing = <String>[];
  for (final n in requested.take(50)) {
    final a = await _resolveArtist(runner, n);
    if (a != null && !artists.contains(a)) {
      artists.add(a);
    } else if (a == null) {
      missing.add(n);
    }
  }
  if (artists.isEmpty) {
    final who = requested.map(_repr).join(', ');
    return {
      'success': false,
      'message': 'No library artist matching ${who.isEmpty ? 'that' : who}.',
    };
  }

  var genres = <String>[];
  for (final g in (args['genres'] as List?) ?? const []) {
    final t = pyLower(pyStrip(_text(g)));
    if (t.isNotEmpty && !genres.contains(t)) genres.add(t);
  }
  var country = pyStrip(_text(args['country'])).toUpperCase();
  if (country.isNotEmpty && cleanCountry(country).isEmpty) {
    return {'success': false, 'error': "'$country' isn't an ISO country code."};
  }
  country = cleanCountry(country);
  final replace =
      (pyTruthy(args['mode']) ? _text(args['mode']) : 'replace') != 'add';
  var useAi = false;
  if (genres.isEmpty && country.isEmpty) {
    if (artists.length != 1) {
      return {
        'success': false,
        'message': 'Give the genres and/or country to set.',
      };
    }
    final cached = await repo.aiResearch(artists.first);
    final p = cached?['payload'] is Map<String, Object?>
        ? cached!['payload']! as Map<String, Object?>
        : const <String, Object?>{};
    if (cached == null || p['status'] != 'ok') {
      return {
        'success': false,
        'message':
            'No suggestion on file for ${artists.first}; research it first '
            'or give genres/country.',
      };
    }
    genres = [for (final x in (p['genres'] as List?) ?? const []) _text(x)];
    country = pyTruthy(p['country']) ? _text(p['country']) : '';
    useAi = true;
  }

  var what = genres.join(', ');
  if (country.isNotEmpty) what = '$what${what.isNotEmpty ? ' · ' : ''}$country';
  final who = artists.length == 1 ? artists.first : '${artists.length} artists';
  final verb = replace && genres.isNotEmpty ? 'set' : 'tag';

  Future<AssistantResponse> doIt() async {
    if (useAi) {
      await researcher.acceptSuggestion(
        artists.first,
        genres: genres,
        country: country.isEmpty ? null : country,
      );
    } else if (replace && genres.isNotEmpty) {
      await repo.setArtistsGenres(
        artists,
        genres,
        country: country.isEmpty ? null : country,
      );
    } else {
      await repo.bulkTagArtists(
        artists,
        genre: genres.isEmpty ? null : genres.join(','),
        country: country.isEmpty ? null : country,
      );
    }
    unawaited(refresh());
    return AssistantResponse(
      spoken:
          'Done, sir. $who ${artists.length == 1 ? 'is' : 'are'} now $what.',
      displayed: 'Saved **$who** → $what. Auto-Play will use it from now on.',
    );
  }

  final listed = artists.take(8).join(', ') + (artists.length > 8 ? '…' : '');
  final note = missing.isNotEmpty
      ? " (couldn't find: ${missing.join(', ')})"
      : '';
  return runner.agentRequestConfirmation(
    spoken: 'Shall I $verb $who as $what, sir? Auto-Play will use it.',
    displayed:
        "${verb == 'set' ? 'Set' : 'Tag'} **$listed** as **$what**"
        "${verb == 'set' ? ' (replacing their genres)' : ''}?$note "
        'This changes what Auto-Play queues around them.',
    onYes: doIt,
  );
}
