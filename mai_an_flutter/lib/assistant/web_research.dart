import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../core/app_config.dart';
import '../core/py_json.dart';
import '../core/py_re.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import 'llm_engine.dart' show defaultGeminiModel;
import 'web_text.dart';

/// `web_research.py` and the search/grounding half of `metadata_ai.py`:
/// general web lookups for Jarvis (DuckDuckGo Lite snippets, Wikipedia search,
/// and Gemini `generateContent` with `googleSearch` grounding), with the
/// 30-minute in-memory cache and session-scoped Google-off latch.

const kDdgLiteUrl = 'https://lite.duckduckgo.com/lite/';
const kWikiApiUrl = 'https://en.wikipedia.org/w/api.php';
const kGeminiGenerateUrlTemplate =
    'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent';

/// `metadata_ai._BROWSER_UA`: DuckDuckGo Lite answers 403 to default UAs.
const kBrowserUa =
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36';

/// `metadata_ai._WIKI_UA`: Wikimedia asks API clients to identify themselves.
const kWikiUa = 'StreamripApp/2.0 (personal music library metadata)';

const _maxResults = 6;
const _defaultTimeout = Duration(seconds: 4);
const _cacheTtlS = 30 * 60.0;
const _maxCacheSize = 200;
const _maxRetryDelayS = 60.0;

final _wikiTagRe = PyRe(r'<[^<]+?>');
final _retryDelayRe = RegExp(r'"retryDelay"\s*:\s*"([0-9]+(?:\.[0-9]+)?)s"');

/// `metadata_ai.ResearchUnavailable`.
class ResearchUnavailable implements Exception {
  const ResearchUnavailable(this.message);
  final String message;

  @override
  String toString() => message;
}

/// `metadata_ai._http_reason`.
String geminiHttpReason(int status, String text, String model) {
  final low = pyLower(text);
  if (status == 429) {
    return 'Gemini quota reached — try again later.';
  }
  if (status == 401 || status == 403 || low.contains('api key')) {
    return 'Gemini rejected the API key.';
  }
  if (status == 404 || low.contains('not found')) {
    return "Model '$model' isn't available for this key — pick another in Settings → AI Assistant.";
  }
  if (low.contains('google_search') ||
      low.contains('googlesearch') ||
      low.contains('tool')) {
    return "Model '$model' can't use Google Search — switch Research Paradigm to 'Smart Hybrid' or 'Training Weights' in Settings → AI Assistant.";
  }
  return 'Gemini returned HTTP $status.';
}

/// `metadata_ai._retry_delay`.
double? parseRetryDelay(String text) {
  final m = _retryDelayRe.firstMatch(text);
  if (m == null) return null;
  return double.tryParse(m.group(1) ?? '');
}

/// `metadata_ai._schema_rejected`.
bool schemaRejected(String text) {
  final low = pyLower(text);
  return low.contains('schema') ||
      low.contains('propertyordering') ||
      low.contains('response_mime_type') ||
      low.contains('responsemimetype');
}

/// `metadata_ai._response_text`: concatenates non-thought text parts from the
/// first candidate.
String geminiResponseText(Map<String, Object?> data) {
  final cands = data['candidates'];
  if (cands is! List || cands.isEmpty) return '';
  final first = cands.first;
  if (first is! Map<String, Object?>) return '';
  final content = first['content'];
  if (content is! Map<String, Object?>) return '';
  final parts = content['parts'];
  if (parts is! List) return '';
  final out = StringBuffer();
  for (final p in parts) {
    if (p is! Map<String, Object?>) continue;
    if (pyTruthy(p['thought'])) continue;
    final t = p['text'];
    if (t is String) out.write(t);
  }
  return out.toString();
}

/// `metadata_ai._grounding`: extracts deduplicated web sources (up to 8) and
/// search queries (up to 4) from `candidates[0].groundingMetadata`.
(List<Map<String, String>>, List<String>) geminiGrounding(
  Map<String, Object?> data,
) {
  final cands = data['candidates'];
  final first = (cands is List && cands.isNotEmpty) ? cands.first : null;
  final meta = (first is Map<String, Object?>)
      ? (first['groundingMetadata'] as Map<String, Object?>? ??
            const <String, Object?>{})
      : const <String, Object?>{};
  final sources = <Map<String, String>>[];
  final seen = <String>{};
  final chunks = meta['groundingChunks'];
  if (chunks is List) {
    for (final ch in chunks) {
      if (ch is! Map<String, Object?>) continue;
      final web = ch['web'];
      if (web is! Map<String, Object?>) continue;
      final uri = web['uri'] is String ? web['uri']! as String : '';
      final title = web['title'] is String ? web['title']! as String : '';
      if (uri.isEmpty || seen.contains(uri)) continue;
      seen.add(uri);
      sources.add({'title': title.isNotEmpty ? title : uri, 'uri': uri});
      if (sources.length >= 8) break;
    }
  }
  final queries = <String>[];
  final rawQ = meta['webSearchQueries'];
  if (rawQ is List) {
    for (final q in rawQ) {
      if (q is String && q.isNotEmpty) {
        queries.add(q);
        if (queries.length >= 4) break;
      }
    }
  }
  return (sources, queries);
}

/// `metadata_ai._parse_ddg_lite`: extracts `{title, uri, snippet}` rows from a
/// DuckDuckGo Lite HTML page by pairing each `a.result-link` with the
/// `td.result-snippet` that follows it. Ads (DuckDuckGo redirect links without
/// `uddg`) and blank snippets are skipped.
List<Map<String, String>> parseDdgLite(String html) {
  final out = <Map<String, String>>[];
  Map<String, String>? current;
  String? capture; // 'title' | 'snippet'
  final buf = StringBuffer();

  final n = html.length;
  var i = 0;

  void emitData(String raw) {
    if (capture != null && raw.isNotEmpty) {
      buf.write(_unescapeHtml(raw));
    }
  }

  void handleStartTag(String tag, Map<String, String> attrs) {
    final classes = _splitPyWs(attrs['class'] ?? '');
    if (tag == 'a' && classes.contains('result-link')) {
      var href = attrs['href'] ?? '';
      if (href.startsWith('//')) {
        href = 'https:$href';
      }
      final parsed = _parseUrlForDdg(href);
      if (parsed.netloc.contains('duckduckgo.com')) {
        final target = _parseQsFirst(parsed.query, 'uddg');
        href = target ?? '';
      }
      current = href.isNotEmpty ? {'uri': href} : null;
      capture = 'title';
      buf.clear();
    } else if (tag == 'td' &&
        classes.contains('result-snippet') &&
        current != null) {
      capture = 'snippet';
      buf.clear();
    }
  }

  void handleEndTag(String tag) {
    if ((capture == 'title' && tag == 'a') ||
        (capture == 'snippet' && tag == 'td')) {
      final text = _splitPyWs(buf.toString()).join(' ');
      if (current != null) {
        current![capture!] = text;
        if (capture == 'snippet') {
          if (text.isNotEmpty) {
            out.add(current!);
          }
          current = null;
        }
      }
      capture = null;
    }
  }

  while (i < n) {
    final lt = html.indexOf('<', i);
    if (lt < 0) {
      emitData(html.substring(i));
      break;
    }
    if (lt > i) {
      emitData(html.substring(i, lt));
    }
    if (lt + 1 >= n) {
      emitData('<');
      break;
    }
    final next = html.codeUnitAt(lt + 1);

    // Comment or declaration: <!-- ... --> or <! ... >
    if (next == 0x21 /* ! */ ) {
      if (html.startsWith('<!--', lt)) {
        final end = html.indexOf('-->', lt + 4);
        i = end < 0 ? n : end + 3;
      } else {
        final end = html.indexOf('>', lt + 2);
        i = end < 0 ? n : end + 1;
      }
      continue;
    }

    // Processing instruction: <? ... >
    if (next == 0x3f /* ? */ ) {
      final end = html.indexOf('>', lt + 2);
      i = end < 0 ? n : end + 1;
      continue;
    }

    // End tag: </name ... >
    if (next == 0x2f /* / */ ) {
      var j = lt + 2;
      while (j < n && _isTagNameChar(html.codeUnitAt(j))) {
        j++;
      }
      final tag = html.substring(lt + 2, j).toLowerCase();
      final gt = html.indexOf('>', j);
      if (gt < 0) {
        break;
      }
      if (tag.isNotEmpty) {
        handleEndTag(tag);
      }
      i = gt + 1;
      continue;
    }

    // Start tag: <name attrs...>
    if (_isAsciiAlpha(next)) {
      var j = lt + 1;
      while (j < n && _isTagNameChar(html.codeUnitAt(j))) {
        j++;
      }
      final tag = html.substring(lt + 1, j).toLowerCase();
      final attrs = <String, String>{};
      var closed = false;
      while (j < n) {
        while (j < n && _isHtmlSpace(html.codeUnitAt(j))) {
          j++;
        }
        if (j >= n) break;
        final c = html.codeUnitAt(j);
        if (c == 0x3e /* > */ ) {
          j++;
          closed = true;
          break;
        }
        if (c == 0x2f /* / */ ) {
          j++;
          if (j < n && html.codeUnitAt(j) == 0x3e) {
            j++;
            closed = true;
            break;
          }
          continue;
        }
        final attrStart = j;
        while (j < n &&
            !_isHtmlSpace(html.codeUnitAt(j)) &&
            html.codeUnitAt(j) != 0x3d &&
            html.codeUnitAt(j) != 0x3e &&
            html.codeUnitAt(j) != 0x2f) {
          j++;
        }
        final attrName = html.substring(attrStart, j).toLowerCase();
        while (j < n && _isHtmlSpace(html.codeUnitAt(j))) {
          j++;
        }
        var attrVal = '';
        if (j < n && html.codeUnitAt(j) == 0x3d /* = */ ) {
          j++;
          while (j < n && _isHtmlSpace(html.codeUnitAt(j))) {
            j++;
          }
          if (j < n &&
              (html.codeUnitAt(j) == 0x22 /* " */ ||
                  html.codeUnitAt(j) == 0x27 /* ' */ )) {
            final quote = html.codeUnitAt(j);
            j++;
            final vStart = j;
            while (j < n && html.codeUnitAt(j) != quote) {
              j++;
            }
            attrVal = _unescapeHtml(html.substring(vStart, j));
            if (j < n) j++;
          } else {
            final vStart = j;
            while (j < n &&
                !_isHtmlSpace(html.codeUnitAt(j)) &&
                html.codeUnitAt(j) != 0x3e) {
              j++;
            }
            attrVal = _unescapeHtml(html.substring(vStart, j));
          }
        }
        if (attrName.isNotEmpty) {
          attrs[attrName] = attrVal;
        }
      }
      if (!closed) {
        emitData(html.substring(lt));
        break;
      }
      handleStartTag(tag, attrs);
      i = j;
      if (tag == 'script' || tag == 'style') {
        final closePat = RegExp('</$tag\\s*>', caseSensitive: false);
        final m = closePat.firstMatch(html.substring(i));
        if (m == null) {
          break;
        }
        i += m.end;
      }
      continue;
    }

    // Stray '<' that doesn't start a tag.
    emitData('<');
    i = lt + 1;
  }

  return [
    for (final r in out)
      {'title': r['title'] ?? '', 'uri': r['uri']!, 'snippet': r['snippet']!},
  ];
}

/// Stateful web researcher (owns the 30-minute cache and the session-scoped
/// Google-grounding circuit breaker, matching `web_research.py`'s module
/// globals).
class WebResearcher {
  WebResearcher({
    this.loadConfig,
    double Function()? monotonicSeconds,
    Future<void> Function(Duration)? sleep,
    this.ddgLiteUrl = kDdgLiteUrl,
    this.wikiApiUrl = kWikiApiUrl,
    this.geminiGenerateUrlTemplate = kGeminiGenerateUrlTemplate,
    this.timeout = _defaultTimeout,
    this.googleTimeout = const Duration(seconds: 45),
    this.networkRetryDelay = const Duration(milliseconds: 1500),
  }) : _monotonicSeconds = monotonicSeconds ?? _defaultMonotonicSeconds,
       _sleep = sleep ?? Future<void>.delayed;

  static final Stopwatch _sw = Stopwatch()..start();
  static double _defaultMonotonicSeconds() => _sw.elapsedMicroseconds / 1e6;

  final FutureOr<AssistantConfig?> Function()? loadConfig;
  final double Function() _monotonicSeconds;
  final Future<void> Function(Duration) _sleep;
  final String ddgLiteUrl;
  final String wikiApiUrl;
  final String geminiGenerateUrlTemplate;
  final Duration timeout;
  final Duration googleTimeout;
  final Duration networkRetryDelay;

  final Map<
    (String, String, String, String, String),
    (double, Map<String, Object?>)
  >
  _cache = {};
  String? _googleOffReason;

  String? get googleOffReason => _googleOffReason;

  void clearCache() => _cache.clear();
  void resetGoogleOff() => _googleOffReason = null;

  Future<(String, String)> _loadResearchSettings() async {
    AssistantConfig? cfg;
    try {
      cfg = await loadConfig?.call();
    } catch (_) {
      cfg = null;
    }
    final key = pyStrip(cfg?.geminiApiKey ?? '');
    final rawModel = pyStrip(cfg?.geminiModel ?? '');
    final model = rawModel.isNotEmpty ? rawModel : defaultGeminiModel;
    return (key, model);
  }

  Future<String> _loadResearchMode() async {
    AssistantConfig? cfg;
    try {
      cfg = await loadConfig?.call();
    } catch (_) {
      cfg = null;
    }
    final raw = cfg?.researchMode ?? '';
    return pyLower(pyStrip(raw.isNotEmpty ? raw : 'hybrid'));
  }

  Future<bool> isResearchAvailable() async {
    final (key, _) = await _loadResearchSettings();
    return key.isNotEmpty;
  }

  Map<String, Object?>? _cached((String, String, String, String, String) key) {
    final hit = _cache[key];
    if (hit != null && _monotonicSeconds() - hit.$1 < _cacheTtlS) {
      return hit.$2;
    }
    return null;
  }

  Map<String, Object?> _store(
    (String, String, String, String, String) key,
    Map<String, Object?> value,
  ) {
    if (pyTruthy(value['success'])) {
      _cache[key] = (_monotonicSeconds(), value);
      if (_cache.length > _maxCacheSize) {
        (String, String, String, String, String)? oldestKey;
        var oldestTs = double.infinity;
        for (final entry in _cache.entries) {
          if (entry.value.$1 < oldestTs) {
            oldestTs = entry.value.$1;
            oldestKey = entry.key;
          }
        }
        if (oldestKey != null) _cache.remove(oldestKey);
      }
    }
    return value;
  }

  /// `web_research.search_snippets`: `[{'title', 'uri', 'snippet'}]` from
  /// DuckDuckGo Lite, then Wikipedia search. Returns `[]` when searched and
  /// nothing found; `null` when nothing was reachable.
  Future<List<Map<String, String>>?> searchSnippets(
    String query, {
    int limit = _maxResults,
    Duration? timeoutOverride,
  }) async {
    final limitTimeout = timeoutOverride ?? timeout;
    var reached = false;
    var out = <Map<String, String>>[];

    final http = HttpClient()..connectionTimeout = limitTimeout;
    try {
      try {
        final bodyBytes = utf8.encode('q=${_quotePlus(query)}');
        final (status, text) = await () async {
          final req = await http.postUrl(Uri.parse(ddgLiteUrl));
          req.headers.set('User-Agent', kBrowserUa);
          req.headers.set('Content-Type', 'application/x-www-form-urlencoded');
          req.contentLength = bodyBytes.length;
          req.add(bodyBytes);
          final resp = await req.close();
          return (resp.statusCode, await utf8.decodeStream(resp));
        }().timeout(limitTimeout);
        if (status == 200) {
          reached = true;
          out = parseDdgLite(text).take(limit).toList();
        }
      } catch (_) {}

      if (out.isEmpty) {
        try {
          final sep = wikiApiUrl.contains('?') ? '&' : '?';
          final url =
              '$wikiApiUrl${sep}action=query&list=search'
              '&srsearch=${pyQuote(query, safe: '/?')}&format=json&srlimit=$limit';
          final (status, text, mime) = await () async {
            final req = await http.getUrl(Uri.parse(url));
            req.headers.set('User-Agent', kWikiUa);
            final resp = await req.close();
            return (
              resp.statusCode,
              await utf8.decodeStream(resp),
              resp.headers.contentType?.mimeType ?? '',
            );
          }().timeout(limitTimeout);
          if (status == 200) {
            reached = true;
            if (mime != 'application/json') {
              throw FormatException(
                'Attempt to decode JSON with unexpected mimetype: $mime',
              );
            }
            final data = jsonDecode(text);
            if (data is Map<String, Object?>) {
              final qObj = data['query'];
              final items = qObj is Map<String, Object?>
                  ? qObj['search']
                  : null;
              if (items is List) {
                for (final item in items.take(limit)) {
                  if (item is! Map<String, Object?>) continue;
                  final rawSnip = item['snippet'] is String
                      ? item['snippet']! as String
                      : '';
                  final snip = pyStrip(_wikiTagRe.sub(rawSnip, ''));
                  final title = item['title'] is String
                      ? item['title']! as String
                      : '';
                  if (snip.isNotEmpty) {
                    out.add({
                      'title': '$title - Wikipedia',
                      'uri':
                          'https://en.wikipedia.org/wiki/'
                          '${pyQuote(title.replaceAll(' ', '_'))}',
                      'snippet': snip,
                    });
                  }
                }
              }
            }
          }
        } catch (_) {}
      }
    } finally {
      http.close(force: true);
    }

    return reached ? out : null;
  }

  /// `metadata_ai._generate`: one Gemini `generateContent` POST with optional
  /// `googleSearch` grounding, one retry on 429 `retryDelay`, and one retry on
  /// connection error.
  Future<Map<String, Object?>> generateContent(
    String apiKey,
    String model,
    String prompt, {
    bool useGrounding = false,
    Duration? timeoutOverride,
    Map<String, Object?>? responseSchema,
  }) async {
    final genCfg = <String, Object?>{'temperature': 0.2};
    // `structured=True`: JSON in the schema's shape (never with the
    // googleSearch tool, which not every model accepts alongside it).
    if (responseSchema != null && !useGrounding) {
      genCfg['responseMimeType'] = 'application/json';
      genCfg['responseSchema'] = responseSchema;
    }
    final payload = <String, Object?>{
      'contents': [
        {
          'role': 'user',
          'parts': [
            {'text': prompt},
          ],
        },
      ],
      'generationConfig': genCfg,
      if (useGrounding)
        'tools': [
          {'googleSearch': <String, Object?>{}},
        ],
    };
    final body = utf8.encode(pyJsonDumps(payload));
    final url = geminiGenerateUrlTemplate.replaceAll('{model}', model);
    final reqTimeout = timeoutOverride ?? googleTimeout;
    final retried = <String>{};

    while (true) {
      final http = HttpClient()
        ..connectionTimeout = const Duration(seconds: 10);
      try {
        final HttpClientRequest req;
        try {
          req = await http.postUrl(Uri.parse(url)).timeout(reqTimeout);
        } on SocketException catch (e) {
          if (!retried.contains('network') &&
              !e.message.contains('timed out')) {
            retried.add('network');
            await _sleep(networkRetryDelay);
            continue;
          }
          throw ResearchUnavailable('Network error contacting Gemini: $e');
        }
        final (status, full, mime) = await () async {
          req.headers.set('x-goog-api-key', apiKey);
          req.headers.set('Content-Type', 'application/json');
          req.contentLength = body.length;
          req.add(body);
          final resp = await req.close();
          return (
            resp.statusCode,
            await utf8.decodeStream(resp),
            resp.headers.contentType?.mimeType ?? '',
          );
        }().timeout(reqTimeout);

        if (status == 200) {
          if (mime != 'application/json') {
            throw FormatException(
              'Attempt to decode JSON with unexpected mimetype: $mime',
            );
          }
          final decoded = jsonDecode(full);
          if (decoded is Map<String, Object?>) return decoded;
          throw const FormatException('Gemini reply is not a JSON object');
        }

        final text = _head(full, 300);
        if (status == 429 && !retried.contains('quota')) {
          final delay = parseRetryDelay(full);
          if (delay != null && delay <= _maxRetryDelayS) {
            retried.add('quota');
            await _sleep(Duration(milliseconds: ((delay + 1) * 1000).round()));
            continue;
          }
        }
        if (status == 400 &&
            genCfg.containsKey('responseSchema') &&
            schemaRejected(full)) {
          genCfg.remove('responseMimeType');
          genCfg.remove('responseSchema');
          continue;
        }
        if (status == 400 ||
            status == 401 ||
            status == 403 ||
            status == 404 ||
            status == 429) {
          throw ResearchUnavailable(geminiHttpReason(status, text, model));
        }
        throw StateError('HTTP $status: $text');
      } finally {
        http.close(force: true);
      }
    }
  }

  /// `web_research._ask_google`.
  Future<Map<String, Object?>> _askGoogle(
    String question,
    String context,
  ) async {
    final (key, model) = await _loadResearchSettings();
    if (key.isEmpty) {
      throw const ResearchUnavailable('No Gemini API key set.');
    }
    if (_googleOffReason != null) {
      throw ResearchUnavailable(_googleOffReason!);
    }
    final prompt =
        'Search the web and answer for a music listener, in at most 6 short sentences. '
        'State facts plainly (names, years, places); say so if the sources disagree or '
        "you can't find it. No markdown headings.\n"
        '${context.isNotEmpty ? 'Context: $context\n' : ''}'
        'Question: $question';
    final Map<String, Object?> data;
    try {
      data = await generateContent(
        key,
        model,
        prompt,
        useGrounding: true,
        timeoutOverride: googleTimeout,
      );
    } on ResearchUnavailable catch (exc) {
      final msg = exc.message;
      if (msg.contains("can't use Google Search") ||
          pyLower(msg).contains('quota')) {
        _googleOffReason = msg;
      }
      rethrow;
    }
    final answer = pyStrip(geminiResponseText(data));
    final (sources, queries) = geminiGrounding(data);
    if (answer.isEmpty) {
      throw const ResearchUnavailable('Google returned no answer.');
    }
    return {
      'success': true,
      'backend': 'google',
      'answer': answer,
      'sources': sources,
      'searched': queries,
    };
  }

  /// `web_research.ask`.
  Future<Map<String, Object?>> ask(
    String question, {
    String context = '',
    String searchQuery = '',
  }) async {
    final q = pyStrip(question);
    if (q.isEmpty) {
      return {'success': false, 'error': 'Empty question.'};
    }
    final sq = pyStrip(searchQuery.isNotEmpty ? searchQuery : q);
    final mode = await _loadResearchMode();
    final key = ('ask', pyLower(q), pyLower(sq), context, mode);
    final hit = _cached(key);
    if (hit != null) return hit;

    final wantGoogle = mode == 'google_grounding';
    if (wantGoogle) {
      try {
        final g = await _askGoogle(q, context);
        return _store(key, {...g, 'query': sq});
      } catch (_) {}
    }

    final snippets = await searchSnippets(sq);
    if (snippets != null && snippets.isNotEmpty) {
      return _store(key, {
        'success': true,
        'backend': 'web',
        'query': sq,
        'sources': snippets,
        'note': 'Search snippets — answer from them and name the site.',
      });
    }
    if (!wantGoogle && await isResearchAvailable()) {
      try {
        final g = await _askGoogle(q, context);
        return _store(key, {...g, 'query': sq});
      } catch (_) {}
    }
    if (snippets != null && snippets.isEmpty) {
      return {
        'success': true,
        'backend': 'web',
        'query': sq,
        'sources': <Object?>[],
        'note': 'The web search found nothing for this.',
      };
    }
    return {'success': false, 'error': "The web isn't reachable right now."};
  }

  /// `web_research.related_artists`.
  Future<Map<String, Object?>> relatedArtists(
    String artist,
    List<Map<String, Object?>> libraryArtists,
  ) async {
    final a = pyStrip(artist);
    if (a.isEmpty) {
      return {'success': false, 'error': 'No artist given.'};
    }
    final res = await ask(
      'Which artists are similar to $a? List names.',
      searchQuery: 'artists similar to $a',
      context: 'The listener likes $a and wants similar artists.',
    );
    if (!pyTruthy(res['success'])) return res;

    final parts = <String>[
      res['answer'] is String ? res['answer']! as String : '',
    ];
    final sources = res['sources'];
    if (sources is List) {
      for (final s in sources) {
        if (s is Map) {
          parts.add("${s['title'] ?? ''} ${s['snippet'] ?? ''}");
        }
      }
    }
    final text = parts.join(' ');
    final byName = <String, Map<String, Object?>>{};
    for (final r in libraryArtists) {
      final n = r['name'];
      if (n is String && n.isNotEmpty) {
        byName[n] = r;
      }
    }
    final owned = namesMentioned(text, byName.keys, exclude: [a]);
    return {
      ...res,
      'artist': a,
      'in_your_library': [
        for (final n in owned.take(15))
          {'name': n, 'track_count': byName[n]!['track_count']},
      ],
    };
  }
}

// ── Private helpers ──────────────────────────────────────────────────────────

String _head(String s, int n) {
  final r = s.runes;
  return r.length <= n ? s : String.fromCharCodes(r.take(n));
}

/// `urllib.parse.quote_plus(s)` (default `safe=''`): UTF-8, spaces as `+`.
String _quotePlus(String s) => pyQuote(s, safe: ' ').replaceAll(' ', '+');

List<String> _splitPyWs(String s) {
  final out = <String>[];
  final buf = StringBuffer();
  for (final r in s.runes) {
    if (pyIsSpace(r)) {
      if (buf.isNotEmpty) {
        out.add(buf.toString());
        buf.clear();
      }
    } else {
      buf.writeCharCode(r);
    }
  }
  if (buf.isNotEmpty) out.add(buf.toString());
  return out;
}

bool _isAsciiAlpha(int c) =>
    (c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a);

bool _isTagNameChar(int c) =>
    _isAsciiAlpha(c) ||
    (c >= 0x30 && c <= 0x39) ||
    c == 0x2d /* - */ ||
    c == 0x5f /* _ */ ||
    c == 0x3a /* : */;

bool _isHtmlSpace(int c) =>
    c == 0x20 || c == 0x09 || c == 0x0a || c == 0x0c || c == 0x0d;

({String netloc, String query}) _parseUrlForDdg(String href) {
  var s = href;
  final fragIdx = s.indexOf('#');
  if (fragIdx >= 0) s = s.substring(0, fragIdx);
  var query = '';
  final qIdx = s.indexOf('?');
  if (qIdx >= 0) {
    query = s.substring(qIdx + 1);
    s = s.substring(0, qIdx);
  }
  final schemeEnd = s.indexOf('://');
  var netloc = '';
  if (schemeEnd >= 0) {
    final rest = s.substring(schemeEnd + 3);
    final slash = rest.indexOf('/');
    netloc = slash < 0 ? rest : rest.substring(0, slash);
  }
  return (netloc: netloc, query: query);
}

/// `urllib.parse.parse_qs(query).get(key)[0]` (drops blank values by default).
String? _parseQsFirst(String query, String key) {
  if (query.isEmpty) return null;
  for (final pair in query.split('&')) {
    if (pair.isEmpty) continue;
    final eq = pair.indexOf('=');
    if (eq < 0) continue;
    final k = _unquotePlus(pair.substring(0, eq));
    if (k != key) continue;
    final v = _unquotePlus(pair.substring(eq + 1));
    if (v.isNotEmpty) return v;
  }
  return null;
}

String _unquotePlus(String s) {
  final replaced = s.replaceAll('+', ' ');
  final bytes = <int>[];
  var i = 0;
  while (i < replaced.length) {
    final cu = replaced.codeUnitAt(i);
    if (cu == 0x25 /* % */ && i + 2 < replaced.length) {
      final hex = int.tryParse(replaced.substring(i + 1, i + 3), radix: 16);
      if (hex != null) {
        bytes.add(hex);
        i += 3;
        continue;
      }
    }
    bytes.addAll(utf8.encode(replaced[i]));
    i++;
  }
  return utf8.decode(bytes, allowMalformed: true);
}

final _charRefRe = RegExp(
  r'&(#(?:[0-9]+|[xX][0-9a-fA-F]+)|[a-zA-Z][a-zA-Z0-9]*);?',
);

/// HTML5 character reference decoder for DDG Lite HTML (`convert_charrefs=True`).
String _unescapeHtml(String s) {
  if (!s.contains('&')) return s;
  return s.replaceAllMapped(_charRefRe, (m) {
    final full = m[0]!;
    final body = m[1]!;
    if (body.startsWith('#')) {
      final isHex = body.length > 1 && (body[1] == 'x' || body[1] == 'X');
      final numStr = isHex ? body.substring(2) : body.substring(1);
      final code = int.tryParse(numStr, radix: isHex ? 16 : 10);
      if (code == null) return full;
      return _decodeNumericCharRef(code);
    }
    final hasSemi = full.endsWith(';');
    final named = hasSemi ? _htmlEntities[body] : _htmlLegacyEntities[body];
    return named ?? full;
  });
}

String _decodeNumericCharRef(int code) {
  if (code == 0 || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) {
    return '\uFFFD';
  }
  final mapped = _win1252C1[code];
  return String.fromCharCode(mapped ?? code);
}

const _win1252C1 = <int, int>{
  0x80: 0x20ac,
  0x82: 0x201a,
  0x83: 0x0192,
  0x84: 0x201e,
  0x85: 0x2026,
  0x86: 0x2020,
  0x87: 0x2021,
  0x88: 0x02c6,
  0x89: 0x2030,
  0x8a: 0x0160,
  0x8b: 0x2039,
  0x8c: 0x0152,
  0x8e: 0x017d,
  0x91: 0x2018,
  0x92: 0x2019,
  0x93: 0x201c,
  0x94: 0x201d,
  0x95: 0x2022,
  0x96: 0x2013,
  0x97: 0x2014,
  0x98: 0x02dc,
  0x99: 0x2122,
  0x9a: 0x0161,
  0x9b: 0x203a,
  0x9c: 0x0153,
  0x9e: 0x017e,
  0x9f: 0x0178,
};

const _htmlLegacyEntities = <String, String>{
  'amp': '&',
  'AMP': '&',
  'lt': '<',
  'LT': '<',
  'gt': '>',
  'GT': '>',
  'quot': '"',
  'QUOT': '"',
  'nbsp': '\u00a0',
  'copy': '\u00a9',
  'reg': '\u00ae',
};

const _htmlEntities = <String, String>{
  'amp': '&',
  'AMP': '&',
  'lt': '<',
  'LT': '<',
  'gt': '>',
  'GT': '>',
  'quot': '"',
  'QUOT': '"',
  'apos': "'",
  'nbsp': '\u00a0',
  'iexcl': '\u00a1',
  'cent': '\u00a2',
  'pound': '\u00a3',
  'euro': '\u20ac',
  'copy': '\u00a9',
  'reg': '\u00ae',
  'trade': '\u2122',
  'deg': '\u00b0',
  'plusmn': '\u00b1',
  'middot': '\u00b7',
  'laquo': '\u00ab',
  'raquo': '\u00bb',
  'ndash': '\u2013',
  'mdash': '\u2014',
  'lsquo': '\u2018',
  'rsquo': '\u2019',
  'sbquo': '\u201a',
  'ldquo': '\u201c',
  'rdquo': '\u201d',
  'bdquo': '\u201e',
  'bull': '\u2022',
  'hellip': '\u2026',
  'prime': '\u2032',
  'Prime': '\u2033',
  'lsaquo': '\u2039',
  'rsaquo': '\u203a',
  'times': '\u00d7',
  'divide': '\u00f7',
  'Agrave': '\u00c0',
  'Aacute': '\u00c1',
  'Acirc': '\u00c2',
  'Atilde': '\u00c3',
  'Auml': '\u00c4',
  'Aring': '\u00c5',
  'AElig': '\u00c6',
  'Ccedil': '\u00c7',
  'Egrave': '\u00c8',
  'Eacute': '\u00c9',
  'Ecirc': '\u00ca',
  'Euml': '\u00cb',
  'Igrave': '\u00cc',
  'Iacute': '\u00cd',
  'Icirc': '\u00ce',
  'Iuml': '\u00cf',
  'Ntilde': '\u00d1',
  'Ograve': '\u00d2',
  'Oacute': '\u00d3',
  'Ocirc': '\u00d4',
  'Otilde': '\u00d5',
  'Ouml': '\u00d6',
  'Oslash': '\u00d8',
  'Ugrave': '\u00d9',
  'Uacute': '\u00da',
  'Ucirc': '\u00db',
  'Uuml': '\u00dc',
  'Yacute': '\u00dd',
  'szlig': '\u00df',
  'agrave': '\u00e0',
  'aacute': '\u00e1',
  'acirc': '\u00e2',
  'atilde': '\u00e3',
  'auml': '\u00e4',
  'aring': '\u00e5',
  'aelig': '\u00e6',
  'ccedil': '\u00e7',
  'egrave': '\u00e8',
  'eacute': '\u00e9',
  'ecirc': '\u00ea',
  'euml': '\u00eb',
  'igrave': '\u00ec',
  'iacute': '\u00ed',
  'icirc': '\u00ee',
  'iuml': '\u00ef',
  'ntilde': '\u00f1',
  'ograve': '\u00f2',
  'oacute': '\u00f3',
  'ocirc': '\u00f4',
  'otilde': '\u00f5',
  'ouml': '\u00f6',
  'oslash': '\u00f8',
  'ugrave': '\u00f9',
  'uacute': '\u00fa',
  'ucirc': '\u00fb',
  'uuml': '\u00fc',
  'yacute': '\u00fd',
  'yuml': '\u00ff',
  'OElig': '\u0152',
  'oelig': '\u0153',
  'Scaron': '\u0160',
  'scaron': '\u0161',
  'Yuml': '\u0178',
  'larr': '\u2190',
  'uarr': '\u2191',
  'rarr': '\u2192',
  'darr': '\u2193',
  'harr': '\u2194',
  'spades': '\u2660',
  'clubs': '\u2663',
  'hearts': '\u2665',
  'diams': '\u2666',
};
