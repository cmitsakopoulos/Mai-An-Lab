import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../assistant/llm_engine.dart' show defaultGeminiModel;
import '../assistant/web_research.dart'
    show
        ResearchUnavailable,
        WebResearcher,
        geminiGrounding,
        geminiResponseText,
        kBrowserUa,
        kDdgLiteUrl,
        kWikiUa,
        parseDdgLite;
import '../core/app_config.dart' show AssistantConfig;
import '../core/py_re.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../data/assistant_queries.dart' show cleanSourceTags;
import '../data/library_repository.dart';

/// `metadata_ai.py`: grounded web research for artists the enrichment
/// cascade can't place. Answers are SUGGESTIONS in `artist_ai_research`
/// until the user accepts one; a country needs backing from the web
/// snippets or the user's own titles' script.

const kAiOrigin = 'ai';
const _maxGenres = 3;
const kVocabInPrompt = 60;
const kBatchSpacing = Duration(seconds: 4);
const kResearchBatchSize = 12;
const _searchSpacing = Duration(seconds: 1);
const _maxSnippets = 4;

/// ISO 3166-1 alpha-2 plus XK.
final kIsoAlpha2 = {
  ...'''
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW XK
'''
      .split(RegExp(r'\s+'))
      .where((s) => s.isNotEmpty),
};
const _countryAliases = {'UK': 'GB', 'EL': 'GR', 'EN': 'GB'};
const _confidence = ['high', 'medium', 'low'];

const _genericTitles = {
  'intro', 'outro', 'interlude', 'skit', 'live', 'remix', 'remixes', //
  'untitled', 'greatest hits', 'best of', 'the best of', 'singles', 'demo',
  'acoustic',
};

const _countryWords = <String, List<String>>{
  'GR': [
    'greek', 'greece', 'hellenic', 'ελληνας', 'ελληνιδα', 'ελληνικ', //
    'ελλαδα',
  ],
  'CY': ['cypriot', 'cyprus', 'κυπρ'],
  'US': ['american', 'united states', 'usa'],
  'GB': [
    'british', 'united kingdom', 'england', 'scottish', 'scotland', //
    'welsh', 'wales',
  ],
  'IE': ['irish', 'ireland'],
  'CA': ['canadian', 'canada'],
  'AU': ['australian', 'australia'],
  'NZ': ['new zealand'],
  'FR': ['france'],
  'DE': ['german', 'germany'],
  'IT': ['italian', 'italy'],
  'ES': ['spain', 'spaniard'],
  'PT': ['portugal'],
  'NL': ['dutch', 'netherlands'],
  'BE': ['belgian', 'belgium'],
  'AT': ['austrian', 'austria'],
  'CH': ['swiss', 'switzerland'],
  'SE': ['swedish', 'sweden'],
  'NO': ['norwegian', 'norway'],
  'DK': ['danish', 'denmark'],
  'FI': ['finnish', 'finland'],
  'IS': ['icelandic', 'iceland'],
  'PL': ['polish', 'poland'],
  'CZ': ['czech'],
  'HU': ['hungarian', 'hungary'],
  'RO': ['romanian', 'romania'],
  'BG': ['bulgarian', 'bulgaria'],
  'RS': ['serbian', 'serbia'],
  'HR': ['croatian', 'croatia'],
  'AL': ['albanian', 'albania'],
  'MK': ['north macedonia'],
  'TR': ['turkish', 'turkey'],
  'RU': ['russian', 'russia'],
  'UA': ['ukrainian', 'ukraine'],
  'IL': ['israeli', 'israel'],
  'LB': ['lebanese', 'lebanon'],
  'EG': ['egyptian', 'egypt'],
  'MA': ['moroccan', 'morocco'],
  'DZ': ['algerian', 'algeria'],
  'NG': ['nigerian', 'nigeria'],
  'GH': ['ghanaian', 'ghana'],
  'ZA': ['south african'],
  'JM': ['jamaican', 'jamaica'],
  'BR': ['brazilian', 'brazil'],
  'AR': ['argentine', 'argentinian', 'argentina'],
  'MX': ['mexican', 'mexico'],
  'CO': ['colombian', 'colombia'],
  'PR': ['puerto rican', 'puerto rico'],
  'CU': ['cuban', 'cuba'],
  'JP': ['japanese', 'japan'],
  'KR': ['korean', 'korea'],
  'CN': ['chinese', 'china'],
  'IN': ['india'],
};

const _countryLanguageWords = <String, List<String>>{
  'ES': ['spanish', 'espanol', 'espanola'],
  'GB': ['english'],
  'FR': ['french', 'francais', 'francaise'],
  'PT': ['portuguese'],
};

const _musicCues = [
  'album', 'song', 'single', 'track', 'singer', 'rapper', 'band', 'music', //
  'musician', 'record', 'lyrics', 'discography', 'ep', 'mixtape', 'spotify',
  'τραγουδ', 'αλμπουμ', 'δισκ', 'ραπερ', 'μουσικ', 'cancion', 'chanson',
];
final _musicCueRe = PyRe('(?<!\\w)(${_musicCues.join('|')})');

const _countryNoise = [
  'latin american', 'african american', 'north american', 'south american', //
  'greek mythology', 'greek god',
];

/// `_SCRIPT_COUNTRIES`: a script only a few countries write, as rune ranges.
const _scriptCountries = <(List<(int, int)>, Set<String>)>[
  ([(0x0370, 0x03ff), (0x1f00, 0x1fff)], {'GR', 'CY'}),
  ([(0xac00, 0xd7af)], {'KR'}),
  ([(0x3040, 0x30ff)], {'JP'}),
  ([(0x0590, 0x05ff)], {'IL'}),
  ([(0x0e00, 0x0e7f)], {'TH'}),
  ([(0x10a0, 0x10ff)], {'GE'}),
  ([(0x0530, 0x058f)], {'AM'}),
];

bool _hasScript(String text, List<(int, int)> ranges) {
  for (final r in text.runes) {
    for (final (lo, hi) in ranges) {
      if (lo <= r && r <= hi) return true;
    }
  }
  return false;
}

// ── normalisation & corroboration ───────────────────────────────────────────

final _nonWord = PyRe(r'[^\w]+');

/// `_norm`: lower-case, accent-free, punctuation-free.
String aiNorm(Object? s) {
  final str = s == null ? '' : (s is String ? s : pyStr(s));
  final folded = _nonWord.sub(nfkdCasefoldStrip(str), ' ');
  return folded.split(' ').where((w) => w.isNotEmpty).join(' ');
}

String _str(Object? v) => v is String ? v : (pyTruthy(v) ? '$v' : '');

/// `_search_queries`: the artist with their biggest album, then the name.
List<String> searchQueries(String artist, Map<String, Object?>? evidence) {
  final queries = <String>[];
  final own = aiNorm(artist);
  for (final a in (evidence?['albums'] as List?) ?? const []) {
    final title = pyStrip(_str(a is Map ? a['title'] : null));
    final t = aiNorm(title);
    if (t.runes.length >= 3 && t != own && !_genericTitles.contains(t)) {
      queries.add('$artist $title album');
      break;
    }
  }
  queries.add('$artist music artist');
  return queries;
}

String _domain(String uri) {
  String host;
  try {
    host = Uri.parse(uri).host.toLowerCase();
  } catch (_) {
    host = '';
  }
  return host.startsWith('www.') ? host.substring(4) : host;
}

Iterable<String> _evidenceTitles(Map<String, Object?> evidence) sync* {
  for (final a in (evidence['albums'] as List?) ?? const []) {
    yield _str(a is Map ? a['title'] : null);
  }
  for (final t in (evidence['tracks'] as List?) ?? const []) {
    yield _str(t);
  }
}

/// `on_target_snippets`: snippets naming one of the user's titles on a
/// page about music.
List<Map<String, String>> onTargetSnippets(
  List<Map<String, String>> snippets,
  Map<String, Object?> evidence,
) {
  final own = aiNorm(evidence['artist']);
  final titles = <String>{};
  for (final t in _evidenceTitles(evidence)) {
    final n = aiNorm(t);
    if (n.runes.length >= 4 && n != own && !_genericTitles.contains(n)) {
      titles.add(n);
    }
  }
  return [
    for (final s in snippets)
      if (() {
        final text = ' ${aiNorm('${s['title'] ?? ''} ${s['snippet'] ?? ''}')} ';
        return titles.any((t) => text.contains(' $t ')) &&
            _musicCueRe.search(text) != null;
      }())
        s,
  ];
}

final _wordPatterns = <String, PyRe>{};

PyRe _wordPattern(String w) => _wordPatterns.putIfAbsent(w, () {
  final esc = aiNorm(
    w,
  ).replaceAllMapped(RegExp(r'[\\^$.|?*+()\[\]{}]'), (m) => '\\${m[0]}');
  return PyRe('(?<!\\w)$esc${w == 'usa' ? r'(?!\w)' : ''}');
});

Set<String> _wordsFound(String text, Map<String, List<String>> table) {
  final found = <String>{};
  for (final MapEntry(key: cc, value: words) in table.entries) {
    for (final w in words) {
      if (_wordPattern(w).search(text) != null) {
        found.add(cc);
        break;
      }
    }
  }
  return found;
}

/// `_countries_named`.
Set<String> countriesNamed(String text) {
  var t = aiNorm(text);
  for (final phrase in _countryNoise) {
    t = t.replaceAll(phrase, ' ');
  }
  final named = _wordsFound(t, _countryWords);
  return named.isNotEmpty ? named : _wordsFound(t, _countryLanguageWords);
}

/// `country_basis`: 'web', 'script', 'contradicted' or ''.
String countryBasis(
  String country,
  List<Map<String, String>> snippets,
  Map<String, Object?> evidence,
) {
  if (country.isEmpty) return '';
  final onTarget = onTargetSnippets(snippets, evidence);
  final pool = onTarget.isNotEmpty ? onTarget : snippets;
  final named = <String>{};
  for (final s in pool) {
    named.addAll(countriesNamed('${s['title'] ?? ''} ${s['snippet'] ?? ''}'));
  }
  if (named.contains(country)) return 'web';
  final titles = _evidenceTitles(evidence).join(' ');
  for (final (ranges, ccs) in _scriptCountries) {
    if (ccs.contains(country) && _hasScript(titles, ranges)) return 'script';
  }
  return named.isNotEmpty ? 'contradicted' : '';
}

// ── prompt ──────────────────────────────────────────────────────────────────

String _artistBlock(
  int i,
  Map<String, Object?> ev,
  List<Map<String, String>> snippets,
  bool showSnippets,
) {
  String orNone(String s, String fallback) => s.isEmpty ? fallback : s;
  final albums = orNone(
    [
      for (final a in (ev['albums'] as List?) ?? const [])
        if (a is Map)
          pyTruthy(a['year'])
              ? '${_str(a['title'])} (${pyStr(a['year'])})'
              : _str(a['title']),
    ].join('; '),
    'none',
  );
  final tracks = orNone(
    [for (final t in (ev['tracks'] as List?) ?? const []) _str(t)].join('; '),
    'none',
  );
  final tags = orNone(
    [
      for (final t in (ev['file_tags'] as List?) ?? const []) _str(t),
    ].join(', '),
    'none',
  );
  final known = <String>[];
  if (pyTruthy(ev['known_country'])) {
    known.add('country ${_str(ev['known_country'])}');
  }
  if (pyTruthy(ev['known_area'])) known.add('area ${_str(ev['known_area'])}');
  if (pyTruthy(ev['known_genres'])) {
    known.add(
      'genres ${[for (final g in ev['known_genres']! as List) _str(g)].join(', ')}',
    );
  }
  final lines = [
    'ARTIST $i: ${_str(ev['artist'])}',
    '- Albums: $albums',
    '- Track titles: $tracks',
    '- Genre tags in the files: $tags',
    '- Already known: ${orNone(known.join('; '), 'nothing')}',
  ];
  if (pyTruthy(ev['disputed'])) {
    lines.add(
      "- The library's current answer may be wrong: ${_str(ev['disputed'])}. "
      'Check it rather than repeating it.',
    );
  }
  if (showSnippets) {
    if (snippets.isNotEmpty) {
      lines.add('- Live web search snippets:');
      for (final s in snippets) {
        final d = _domain(s['uri'] ?? '');
        lines.add(
          '  - [${d.isEmpty ? 'web' : d}] ${s['title'] ?? ''}: '
          '${s['snippet'] ?? ''}',
        );
      }
    } else {
      lines.add('- Live web search snippets: none found');
    }
  }
  return lines.join('\n');
}

/// `build_prompt`: one prompt for one or more artists, numbered from 1.
String buildResearchPrompt(
  List<(Map<String, Object?>, List<Map<String, String>>)> items,
  List<String> vocabulary, {
  String mode = 'hybrid',
}) {
  final v = vocabulary.take(kVocabInPrompt).join(', ');
  final vocab = v.isEmpty ? '(empty library vocabulary)' : v;
  final anySnippets = items.any((e) => e.$2.isNotEmpty);
  final String instruction;
  if (mode == 'google_grounding') {
    instruction = 'Search the web.';
  } else if (anySnippets) {
    instruction =
        'Use the live web search snippets given with each artist and your '
        'music knowledge to identify them.';
  } else {
    instruction =
        'Use your internal training knowledge (global/regional music scenes, '
        'forums, and discographies) to identify each artist. Match strictly '
        "against the user's files.";
  }
  final showSnippets = mode == 'hybrid' || mode == 'web_snippets';
  final blocks = [
    for (var i = 0; i < items.length; i++)
      _artistBlock(i + 1, items[i].$1, items[i].$2, showSnippets),
  ].join('\n\n');
  return 'You identify music artists for a personal music library. $instruction\n'
      '\n'
      "Each artist comes with the user's own files. Use them to make sure you have the RIGHT artist — many acts share a name.\n"
      '\n'
      '$blocks\n'
      '\n'
      'For each artist, find their main genres and country of origin.\n'
      '- genres: up to $_maxGenres, lower-case, most specific first. Prefer these words the library already uses: $vocab. Use a different word only if none of those fit.\n'
      '- country: ISO 3166-1 alpha-2 code of where the artist is from (e.g. GR, US, GB). For a group, where it formed.\n'
      "- If an artist cannot be identified or does not match the user's tracks/albums, set identified to false for them. A blank answer is far better than a guess: wrong tags corrupt the user's auto-play.\n"
      '\n'
      'Reply with ONLY this JSON object, one entry per artist, "id" being the artist\'s number:\n'
      '{"artists": [{"id": 1, "identified": true, "matched_as": "the artist as the sources describe them", "summary": "one short sentence, e.g. Greek rapper from Athens, active since 2015", "genres": [], "country": "", "confidence": "high|medium|low"}]}';
}

/// `_ANSWER_SCHEMA`.
const kAnswerSchema = <String, Object?>{
  'type': 'OBJECT',
  'properties': {
    'artists': {
      'type': 'ARRAY',
      'items': {
        'type': 'OBJECT',
        'properties': {
          'id': {'type': 'INTEGER'},
          'identified': {'type': 'BOOLEAN'},
          'matched_as': {'type': 'STRING'},
          'summary': {'type': 'STRING'},
          'genres': {
            'type': 'ARRAY',
            'items': {'type': 'STRING'},
          },
          'country': {'type': 'STRING'},
          'confidence': {
            'type': 'STRING',
            'enum': ['high', 'medium', 'low'],
          },
        },
        'required': ['id', 'identified', 'genres', 'country', 'confidence'],
        'propertyOrdering': [
          'id',
          'identified',
          'matched_as',
          'summary',
          'genres',
          'country',
          'confidence',
        ],
      },
    },
  },
  'required': ['artists'],
};

// ── parsing & validation ────────────────────────────────────────────────────

/// `parse_answer`: the model's JSON object, tolerating fences and prose.
Map<String, Object?>? parseAnswer(String? text) {
  if (text == null || text.isEmpty) return null;
  final i = text.indexOf('{');
  final j = text.lastIndexOf('}');
  if (i < 0 || j < i) return null;
  try {
    final obj = jsonDecode(text.substring(i, j + 1));
    return obj is Map<String, Object?> ? obj : null;
  } catch (_) {
    return null;
  }
}

/// Python's `int(x)`, null where it raises.
int? _pyInt(Object? v) => switch (v) {
  final bool b => b ? 1 : 0,
  final int i => i,
  final double d => d.isFinite ? d.truncate() : null,
  final String s => int.tryParse(pyStrip(s).replaceAll('_', '')),
  _ => null,
};

String _stripFences(String text) {
  var s = pyStrip(text);
  if (s.startsWith('```')) {
    s = s.substring(3);
    if (s.startsWith('json')) s = s.substring(4);
    var k = 0;
    final runes = s.runes.toList();
    while (k < runes.length && pyIsSpace(runes[k])) {
      k++;
    }
    s = String.fromCharCodes(runes.sublist(k));
  }
  if (s.endsWith('```')) {
    final runes = s.substring(0, s.length - 3).runes.toList();
    var k = runes.length;
    while (k > 0 && pyIsSpace(runes[k - 1])) {
      k--;
    }
    s = String.fromCharCodes(runes.sublist(0, k));
  }
  return s;
}

/// `parse_answers`: {artist number: answer} from a reply to an n-artist
/// prompt.
Map<int, Map<String, Object?>> parseAnswers(String? text, int n) {
  if (text == null || text.isEmpty) return {};
  Object? entries;
  final stripped = _stripFences(text);
  if (stripped.startsWith('[')) {
    try {
      entries = jsonDecode(stripped);
    } catch (_) {
      entries = null;
    }
  }
  if (entries == null) {
    final obj = parseAnswer(text);
    if (obj == null) return {};
    if (obj['artists'] is List) {
      entries = obj['artists'];
    } else if (n == 1 && obj.containsKey('identified')) {
      entries = [obj];
    } else {
      return {};
    }
  }
  final out = <int, Map<String, Object?>>{};
  final list = entries is List ? entries : const <Object?>[];
  for (var pos = 1; pos <= list.length; pos++) {
    final a = list[pos - 1];
    if (a is! Map<String, Object?>) continue;
    final i = _pyInt(a['id']) ?? pos;
    if (1 <= i && i <= n && !out.containsKey(i)) out[i] = a;
  }
  return out;
}

/// `_clean_country`: a real ISO code, or ''.
String cleanCountry(Object? raw) {
  var cc = pyStrip(_str(raw)).toUpperCase();
  cc = _countryAliases[cc] ?? cc;
  return kIsoAlpha2.contains(cc) ? cc : '';
}

/// `_clean_genres`: (genres, the subset the library has never used).
(List<String>, List<String>) cleanAiGenres(
  Object? raw,
  List<String> vocabulary,
) {
  final list = raw is String ? [raw] : raw;
  if (list is! List) return (const [], const []);
  final toks = cleanSourceTags([
    for (final g in list)
      if (pyTruthy(g)) _str(g),
  ]);
  final vocab = {for (final v in vocabulary) pyLower(v)};
  final genres = <String>[];
  for (final t0 in toks) {
    final t = pyLower(pyStrip(t0));
    if (t.isNotEmpty && t.runes.length <= 40 && !genres.contains(t)) {
      genres.add(t);
    }
  }
  final kept = genres.take(_maxGenres).toList();
  return (
    kept,
    [
      for (final g in kept)
        if (!vocab.contains(g)) g,
    ],
  );
}

String _clip(String s, int n) {
  final r = s.runes.toList();
  return r.length <= n ? s : String.fromCharCodes(r.sublist(0, n));
}

/// `validate`: a raw answer reduced to what the workbench may offer.
Map<String, Object?> validateAnswer(
  Map<String, Object?>? answer,
  List<Map<String, String>> sources,
  List<String> vocabulary, {
  bool allowMemoryCountry = false,
  String? basis,
}) {
  if (answer == null || answer.isEmpty) {
    return {
      'status': 'unparsed',
      'identified': false,
      'genres': <String>[],
      'new_genres': <String>[],
      'country': '',
      'confidence': 'low',
      'summary': '',
      'matched_as': '',
      'country_basis': '',
      'country_withheld': '',
    };
  }
  final identified = pyTruthy(answer['identified']);
  final (genres, fresh) = identified
      ? cleanAiGenres(answer['genres'], vocabulary)
      : (const <String>[], const <String>[]);
  var country = identified ? cleanCountry(answer['country']) : '';
  var conf = pyLower(_str(answer['confidence']));
  if (!_confidence.contains(conf)) conf = 'low';
  basis ??= sources.isNotEmpty ? 'grounding' : '';
  var withheld = '';
  if (country.isNotEmpty) {
    if (basis == 'contradicted') {
      withheld = 'contradicted';
    } else if (const ['grounding', 'web', 'script'].contains(basis)) {
      // backed
    } else if (allowMemoryCountry && conf == 'high') {
      basis = 'memory';
    } else {
      withheld = 'unsupported';
    }
    if (withheld.isNotEmpty) country = '';
  }
  if (sources.isEmpty && !allowMemoryCountry) conf = 'low';
  return {
    'status': genres.isNotEmpty || country.isNotEmpty ? 'ok' : 'unknown',
    'identified': identified,
    'genres': genres,
    'new_genres': fresh,
    'country': country,
    'confidence': conf,
    'summary': _clip(pyStrip(_str(answer['summary'])), 240),
    'matched_as': _clip(pyStrip(_str(answer['matched_as'])), 120),
    'country_basis': country.isNotEmpty ? basis : '',
    'country_withheld': withheld,
  };
}

/// `_worth_retrying`: unparsed, or empty while search was unreachable.
bool worthRetrying(Map<String, Object?> payload) {
  final status = payload['status'];
  return status == 'unparsed' ||
      (status != 'ok' && pyTruthy(payload['search_failed']));
}

double _now() => DateTime.now().microsecondsSinceEpoch / 1e6;

/// `_finish`: one artist's answer corroborated and validated.
Map<String, Object?> finishAnswer(
  Map<String, Object?> evidence,
  Map<String, Object?>? answer,
  List<Map<String, String>> snippets,
  bool searchFailed,
  List<String> vocabulary,
  String mode,
  String model, {
  (List<Map<String, String>>, List<String>) grounding = (const [], const []),
  double Function()? now,
}) {
  final artist = _str(evidence['artist']);
  final (gSources, gQueries) = grounding;
  final List<Map<String, String>> sources;
  final List<String> queries;
  final String? basis;
  final bool grounded;
  if (gSources.isNotEmpty) {
    sources = List.of(gSources);
    queries = List.of(gQueries);
    basis = null;
    grounded = true;
  } else {
    sources = [
      for (final s in snippets)
        if ((s['uri'] ?? '').isNotEmpty)
          {
            'title': (s['title'] ?? '').isNotEmpty ? s['title']! : artist,
            'uri': s['uri']!,
          },
    ];
    queries = snippets.isNotEmpty
        ? searchQueries(artist, evidence).take(1).toList()
        : <String>[];
    final claimed = cleanCountry(answer?['country']);
    basis = countryBasis(claimed, snippets, evidence);
    grounded = onTargetSnippets(snippets, evidence).isNotEmpty;
  }
  final originMode = mode == 'google_grounding'
      ? 'google_grounding'
      : (snippets.isNotEmpty ? 'web_snippets' : 'weights');
  return {
    ...validateAnswer(
      answer,
      sources,
      vocabulary,
      allowMemoryCountry: mode == 'hybrid' || mode == 'weights',
      basis: basis,
    ),
    'artist': artist,
    'sources': sources,
    'queries': queries,
    'grounded': grounded,
    'model': model,
    'researched_at': (now ?? _now)(),
    'origin_mode': originMode,
    'search_failed': searchFailed,
  };
}

// ── web snippets ────────────────────────────────────────────────────────────

/// (status, body) of one HTTP exchange; throws when unreachable.
typedef AiHttp =
    Future<(int, String)> Function(
      String method,
      Uri uri, {
      Map<String, String> headers,
      String? form,
      Duration timeout,
    });

Future<(int, String)> _defaultHttp(
  String method,
  Uri uri, {
  Map<String, String> headers = const {},
  String? form,
  Duration timeout = const Duration(milliseconds: 3500),
}) async {
  final client = HttpClient()..connectionTimeout = timeout;
  try {
    return await () async {
      final req = await client.openUrl(method, uri);
      headers.forEach(req.headers.set);
      if (form != null) {
        final body = utf8.encode(form);
        req.headers.set('Content-Type', 'application/x-www-form-urlencoded');
        req.contentLength = body.length;
        req.add(body);
      }
      final resp = await req.close();
      return (resp.statusCode, await utf8.decodeStream(resp));
    }().timeout(timeout);
  } finally {
    client.close(force: true);
  }
}

final _wikiTag = PyRe(r'<[^<]+?>');

/// `_fetch_web_snippets`: DuckDuckGo Lite for each query until one finds
/// something (stopping when blocked), then Wikipedia search. `[]` when the
/// searches ran and found nothing, null when nothing was reachable.
Future<List<Map<String, String>>?> fetchArtistSnippets(
  String artist,
  Map<String, Object?>? evidence, {
  AiHttp http = _defaultHttp,
  String ddgLiteUrl = kDdgLiteUrl,
  String wikiApiUrl = 'https://en.wikipedia.org/w/api.php',
  Duration timeout = const Duration(milliseconds: 3500),
}) async {
  var snippets = <Map<String, String>>[];
  var reached = false;
  for (final q in searchQueries(artist, evidence)) {
    try {
      final (status, body) = await http(
        'POST',
        Uri.parse(ddgLiteUrl),
        headers: {'User-Agent': kBrowserUa},
        form: 'q=${pyQuote(q, safe: ' ').replaceAll(' ', '+')}',
        timeout: timeout,
      );
      if (status != 200) break;
      reached = true;
      snippets = parseDdgLite(body).take(_maxSnippets).toList();
    } catch (e) {
      debugPrint('DuckDuckGo snippet lookup failed for $artist: $e');
      break;
    }
    if (snippets.isNotEmpty) break;
  }
  if (snippets.isEmpty) {
    try {
      final url =
          '$wikiApiUrl?action=query&list=search'
          '&srsearch=${pyQuote('$artist music')}&format=json';
      final (status, body) = await http(
        'GET',
        Uri.parse(url),
        headers: {'User-Agent': kWikiUa},
        timeout: timeout,
      );
      if (status == 200) {
        reached = true;
        final data = jsonDecode(body);
        final query = data is Map ? data['query'] : null;
        final items = query is Map ? query['search'] : null;
        if (items is List) {
          for (final item in items.take(3)) {
            if (item is! Map) continue;
            final clean = pyStrip(_wikiTag.sub(_str(item['snippet']), ''));
            final title = _str(item['title']);
            if (clean.isNotEmpty) {
              snippets.add({
                'title': '$title - Wikipedia',
                'uri': 'https://en.wikipedia.org/wiki/${pyQuote(title)}',
                'snippet': clean,
              });
            }
          }
        }
      }
    } catch (e) {
      debugPrint('Wikipedia snippet fallback failed for $artist: $e');
    }
  }
  return reached ? snippets : null;
}

// ── research ────────────────────────────────────────────────────────────────

typedef SnippetFetcher =
    Future<List<Map<String, String>>?> Function(
      String artist,
      Map<String, Object?> evidence,
    );

typedef GeminiGenerate =
    Future<Map<String, Object?>> Function(
      String apiKey,
      String model,
      String prompt, {
      required bool useGrounding,
      Map<String, Object?>? responseSchema,
    });

/// `research_artists`' progress: (i, total, artist, result).
typedef ResearchProgress =
    void Function(int i, int total, String name, Map<String, Object?> result);

/// The research flows of `metadata_ai.py` over the port's library.
class MetadataResearcher {
  MetadataResearcher({
    required this.repo,
    required this.loadConfig,
    WebResearcher? web,
    SnippetFetcher? fetchSnippets,
    GeminiGenerate? generate,
    Future<void> Function(Duration)? sleep,
  }) : _fetchSnippets = fetchSnippets ?? fetchArtistSnippets,
       _sleep = sleep ?? Future<void>.delayed,
       _generate =
           generate ??
           ((key, model, prompt, {required useGrounding, responseSchema}) =>
               (web ?? WebResearcher()).generateContent(
                 key,
                 model,
                 prompt,
                 useGrounding: useGrounding,
                 responseSchema: responseSchema,
                 timeoutOverride: const Duration(seconds: 90),
               ));

  final LibraryRepository repo;
  final FutureOr<AssistantConfig?> Function() loadConfig;
  final SnippetFetcher _fetchSnippets;
  final GeminiGenerate _generate;
  final Future<void> Function(Duration) _sleep;

  /// `load_research_settings`: (api key, model).
  Future<(String, String)> settings() async {
    AssistantConfig? cfg;
    try {
      cfg = await loadConfig();
    } catch (e) {
      debugPrint("AI research: couldn't read config: $e");
    }
    final key = pyStrip(cfg?.geminiApiKey ?? '');
    final model = pyStrip(cfg?.geminiModel ?? '');
    return (key, model.isEmpty ? defaultGeminiModel : model);
  }

  /// `load_research_mode`.
  Future<String> mode() async {
    AssistantConfig? cfg;
    try {
      cfg = await loadConfig();
    } catch (_) {}
    final raw = cfg?.researchMode ?? '';
    return pyLower(pyStrip(raw.isEmpty ? 'hybrid' : raw));
  }

  /// `research_available`.
  Future<bool> available() async => (await settings()).$1.isNotEmpty;

  Future<List<String>> _vocabulary() async => [
    for (final v in await repo.genreVocabulary(kVocabInPrompt))
      v['name']! as String,
  ];

  /// `research_evidence_batch`: each artist web-searched, then ONE model
  /// call, then each answer corroborated. Results in input order.
  Future<List<Map<String, Object?>>> researchEvidenceBatch(
    List<Map<String, Object?>> evidences,
    List<String> vocabulary, {
    required String apiKey,
    required String model,
    String mode = 'hybrid',
    Duration searchSpacing = _searchSpacing,
    void Function(String artist)? onSearch,
  }) async {
    if (mode == 'google_grounding' && evidences.length > 1) {
      final out = <Map<String, Object?>>[];
      for (final ev in evidences) {
        out.addAll(
          await researchEvidenceBatch(
            [ev],
            vocabulary,
            apiKey: apiKey,
            model: model,
            mode: mode,
          ),
        );
      }
      return out;
    }
    final gathered =
        <(Map<String, Object?>, List<Map<String, String>>, bool)>[];
    for (var i = 0; i < evidences.length; i++) {
      final ev = evidences[i];
      final artist = _str(ev['artist']);
      var snippets = <Map<String, String>>[];
      var failed = false;
      if (mode == 'hybrid' || mode == 'web_snippets') {
        onSearch?.call(artist);
        if (i > 0 && searchSpacing > Duration.zero) await _sleep(searchSpacing);
        final found = await _fetchSnippets(artist, ev);
        failed = found == null;
        snippets = found ?? [];
        if (snippets.isEmpty) {
          debugPrint(
            'No web snippets for $artist '
            '(${failed ? 'search unreachable' : 'no results'}); '
            'answering from model memory.',
          );
        }
      }
      gathered.add((ev, snippets, failed));
    }
    final prompt = buildResearchPrompt(
      [for (final (ev, sn, _) in gathered) (ev, sn)],
      vocabulary,
      mode: mode,
    );
    final grounding = mode == 'google_grounding';
    final data = await _generate(
      apiKey,
      model,
      prompt,
      useGrounding: grounding,
      responseSchema: grounding ? null : kAnswerSchema,
    );
    final answers = parseAnswers(geminiResponseText(data), gathered.length);
    final g = gathered.length == 1
        ? geminiGrounding(data)
        : (const <Map<String, String>>[], const <String>[]);
    return [
      for (var i = 0; i < gathered.length; i++)
        finishAnswer(
          gathered[i].$1,
          answers[i + 1],
          gathered[i].$2,
          gathered[i].$3,
          vocabulary,
          mode,
          model,
          grounding: g,
        ),
    ];
  }

  void _log(Map<String, Object?> res) => debugPrint(
    'AI research ${res['artist']} (${res['origin_mode']}) → ${res['status']} '
    'genres=${res['genres']} country=${pyTruthy(res['country']) ? res['country'] : '-'} '
    'conf=${res['confidence']} sources=${(res['sources'] as List?)?.length ?? 0}',
  );

  Future<void> _save(String name, Map<String, Object?> result) =>
      repo.saveAiResearch(
        name,
        result,
        state: result['status'] == 'ok' ? 'pending' : 'dismissed',
      );

  /// `research_artist`: one artist researched and cached as a PENDING
  /// suggestion. A cached answer comes back without a call unless [force]
  /// (or it's worth retrying). Throws [ResearchUnavailable] when research
  /// can't run at all.
  Future<Map<String, Object?>> researchArtist(
    String artist, {
    bool force = false,
    String mode = '',
    String dispute = '',
  }) async {
    if (!force) {
      final cached = await repo.aiResearch(artist);
      final payload = cached?['payload'];
      if (payload is Map<String, Object?> && !worthRetrying(payload)) {
        return payload;
      }
    }
    final (key, model) = await settings();
    if (key.isEmpty) {
      throw ResearchUnavailable(
        'Add a Gemini API key in Settings → AI Assistant to research artists.',
      );
    }
    final vocab = await _vocabulary();
    final effective = pyLower(mode.isNotEmpty ? mode : await this.mode());
    final evidence = await repo.artistResearchEvidence(artist);
    if (dispute.isNotEmpty) evidence['disputed'] = dispute;
    final result = (await researchEvidenceBatch(
      [evidence],
      vocab,
      apiKey: key,
      model: model,
      mode: effective,
    )).first;
    result['artist'] = artist;
    _log(result);
    await _save(artist, result);
    return result;
  }

  /// `research_artists`: [batchSize] artists per model call. Returns
  /// {'status', 'total', 'found', 'unknown', 'skipped', 'failed'}; stops at
  /// the first [ResearchUnavailable].
  Future<Map<String, Object?>> researchArtists(
    List<String> names, {
    bool force = false,
    ResearchProgress? progress,
    bool Function()? cancelled,
    Duration spacing = kBatchSpacing,
    String mode = '',
    int batchSize = kResearchBatchSize,
  }) async {
    final (key, model) = await settings();
    final summary = <String, Object?>{
      'status': 'ok',
      'total': names.length,
      'found': 0,
      'unknown': 0,
      'skipped': 0,
      'failed': 0,
    };
    void bump(String k, [int by = 1]) => summary[k] = (summary[k]! as int) + by;
    if (key.isEmpty) {
      summary['status'] =
          'error: Add a Gemini API key in Settings → AI Assistant.';
      return summary;
    }
    final effective = pyLower(mode.isNotEmpty ? mode : await this.mode());
    final doneBefore = force ? <String>{} : await repo.researchedArtistNames();
    final vocab = await _vocabulary();

    var done = 0;
    final todo = <String>[];
    for (final name in names) {
      var skip = false;
      if (doneBefore.contains(pyLower(name))) {
        final cached = await repo.aiResearch(name);
        final payload = cached?['payload'];
        skip = !worthRetrying(
          payload is Map<String, Object?> ? payload : const {},
        );
      }
      if (skip) {
        bump('skipped');
        done++;
        progress?.call(done, names.length, name, {'status': 'skipped'});
      } else {
        todo.add(name);
      }
    }

    final size = batchSize < 1 ? 1 : batchSize;
    for (var start = 0; start < todo.length; start += size) {
      if (cancelled?.call() ?? false) {
        summary['status'] = 'cancelled';
        break;
      }
      if (start > 0 && spacing > Duration.zero) await _sleep(spacing);
      final chunk = todo.sublist(
        start,
        start + size > todo.length ? todo.length : start + size,
      );
      final evidences = [
        for (final n in chunk) await repo.artistResearchEvidence(n),
      ];
      final doneAt = done;
      final List<Map<String, Object?>> results;
      try {
        results = await researchEvidenceBatch(
          evidences,
          vocab,
          apiKey: key,
          model: model,
          mode: effective,
          searchSpacing: spacing < _searchSpacing ? spacing : _searchSpacing,
          onSearch: (artist) => progress?.call(doneAt, names.length, artist, {
            'status': 'searching',
          }),
        );
      } on ResearchUnavailable catch (e) {
        summary['status'] = 'error: ${e.message}';
        break;
      } catch (e) {
        debugPrint('AI research batch failed (${chunk.first}…): $e');
        bump('failed', chunk.length);
        for (final name in chunk) {
          done++;
          progress?.call(done, names.length, name, {'status': 'error'});
        }
        continue;
      }
      for (var i = 0; i < chunk.length; i++) {
        final name = chunk[i];
        final res = results[i]..['artist'] = name;
        _log(res);
        await _save(name, res);
        bump(res['status'] == 'ok' ? 'found' : 'unknown');
        done++;
        progress?.call(done, names.length, name, res);
      }
    }
    return summary;
  }

  /// `accept_suggestion`: the suggestion written as the user's own answer
  /// (sticky), labelled 'ai' in provenance, merged with what the artist
  /// has. The caller refreshes the walk models.
  Future<void> acceptSuggestion(
    String artist, {
    List<String>? genres,
    String? country,
  }) => repo.acceptAiSuggestion(artist, genres: genres, country: country);

  /// `dismiss_suggestion`.
  Future<void> dismissSuggestion(String artist) =>
      repo.setAiResearchState(artist, 'dismissed');
}
