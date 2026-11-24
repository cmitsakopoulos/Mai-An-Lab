import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../core/py_json.dart';
import '../core/py_text.dart';

/// `llm_engine.py`: Jarvis's LLM client. Google Gemini (its
/// OpenAI-compatible endpoint, the user's own key) or a local Ollama /
/// LM Studio server; both speak the OpenAI chat-completions dialect, so one
/// request path serves both, tool calling included. No credentials in code.

const geminiChatUrl =
    'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions';
const geminiModelsUrl =
    'https://generativelanguage.googleapis.com/v1beta/models';

/// `DEFAULT_GEMINI_MODEL`: the one default, read by Settings, the config
/// and the runner.
const defaultGeminiModel = 'gemini-3.5-flash-lite';
const defaultOllamaEndpoint = 'http://localhost:11434/v1';
const defaultOllamaModel = 'llama3.2';

/// Model families that can't hold a text chat.
const _nonChatMarkers = [
  'embedding', 'image', 'tts', 'live', 'audio', 'veo', 'imagen', //
  'robotics', 'computer-use', 'aqa',
];

/// `LLMResponse`.
class LlmResponse {
  LlmResponse({
    this.content = '',
    this.toolCalls = const [],
    this.success = true,
    this.errorMessage,
    this.rawMessage,
  });

  LlmResponse.failure(String message)
    : this(success: false, errorMessage: message);

  final String content;

  /// `{id, name, args}` per call.
  final List<ToolCall> toolCalls;
  final bool success;
  final String? errorMessage;

  /// The provider's assistant message as it came, echoed back unchanged on
  /// the next turn: Gemini's thinking models put an opaque
  /// thought_signature in each function call that must be returned, and a
  /// message rebuilt from [toolCalls] would drop it.
  final Map<String, Object?>? rawMessage;
}

class ToolCall {
  const ToolCall(this.id, this.name, this.args);
  final String id;
  final String name;
  final Map<String, Object?> args;

  Map<String, Object?> toJson() => {'id': id, 'name': name, 'args': args};
}

/// `LLMEngine`.
class LlmEngine {
  LlmEngine({
    this.provider = 'gemini',
    this.apiKey = '',
    String model = defaultGeminiModel,
    String ollamaEndpoint = defaultOllamaEndpoint,
    String ollamaModel = defaultOllamaModel,
    this.geminiUrl = geminiChatUrl,
    this.retryDelay = const Duration(milliseconds: 1500),
    this.timeoutOverride,
  }) : model = model.isEmpty ? defaultGeminiModel : model,
       ollamaEndpoint = _rstripSlash(ollamaEndpoint),
       ollamaModel = ollamaModel.isEmpty ? defaultOllamaModel : ollamaModel;

  final String provider;
  final String apiKey;
  final String model;
  final String ollamaEndpoint;
  final String ollamaModel;

  /// The Gemini endpoint; a test points it at a local server.
  final String geminiUrl;

  /// The wait before the one retry after a failed connection.
  final Duration retryDelay;

  /// Replaces the providers' total limits (30 s Gemini, 45 s local); for
  /// tests.
  final Duration? timeoutOverride;

  static String _rstripSlash(String s) {
    var e = s.length;
    while (e > 0 && s.codeUnitAt(e - 1) == 0x2f) {
      e--;
    }
    return s.substring(0, e);
  }

  /// `is_configured`: enough to attempt a call.
  bool get isConfigured => switch (provider) {
    'gemini' => pyStrip(apiKey).isNotEmpty,
    'ollama' => ollamaEndpoint.isNotEmpty,
    _ => false,
  };

  /// `chat_completion`: one request to the active provider, with [tools]
  /// (plain declarations, [agentTools]) wrapped in the OpenAI function
  /// envelope.
  Future<LlmResponse> chatCompletion(
    List<Map<String, Object?>> messages, {
    List<Map<String, Object?>>? tools,
  }) async {
    if (!isConfigured) {
      return LlmResponse.failure(
        'AI Agent not configured. Set your Gemini API key or local server '
        'in Settings → AI Assistant.',
      );
    }
    if (provider == 'gemini') {
      return _callOpenAiCompatible(
        geminiUrl,
        {
          'Authorization': 'Bearer ${pyStrip(apiKey)}',
          'Content-Type': 'application/json',
        },
        model,
        messages,
        tools,
        timeoutOverride ?? const Duration(seconds: 30),
      );
    }
    return _callOpenAiCompatible(
      '$ollamaEndpoint/chat/completions',
      {'Content-Type': 'application/json'},
      ollamaModel,
      messages,
      tools,
      timeoutOverride ?? const Duration(seconds: 45),
    );
  }

  Future<LlmResponse> _callOpenAiCompatible(
    String url,
    Map<String, String> headers,
    String model,
    List<Map<String, Object?>> messages,
    List<Map<String, Object?>>? tools,
    Duration timeout,
  ) async {
    final payload = <String, Object?>{'model': model, 'messages': messages};
    if (tools != null && tools.isNotEmpty) {
      payload['tools'] = [
        for (final t in tools)
          {
            'type': 'function',
            'function': {
              'name': t['name'],
              'description': t['description'] ?? '',
              'parameters': t['parameters'] ?? <String, Object?>{},
            },
          },
      ];
    }
    // aiohttp's json= is json.dumps with its defaults.
    final body = utf8.encode(pyJsonDumps(payload));

    for (var attempt = 1; ; attempt++) {
      final http = HttpClient()
        ..connectionTimeout = const Duration(seconds: 10);
      try {
        final HttpClientRequest req;
        try {
          req = await http.postUrl(Uri.parse(url)).timeout(timeout);
        } on SocketException catch (e) {
          // aiohttp's ClientConnectorError (refused, DNS): one retry.
          if (attempt == 1 && !e.message.contains('timed out')) {
            await Future<void>.delayed(retryDelay);
            continue;
          }
          return LlmResponse.failure('Network error: $e');
        }
        return await _exchange(req, headers, body).timeout(timeout);
      } on TimeoutException {
        // str(asyncio.TimeoutError()) is empty.
        return LlmResponse.failure('Network error: ');
      } catch (e) {
        return LlmResponse.failure('Network error: $e');
      } finally {
        http.close(force: true);
      }
    }
  }

  Future<LlmResponse> _exchange(
    HttpClientRequest req,
    Map<String, String> headers,
    List<int> body,
  ) async {
    headers.forEach(req.headers.set);
    req.contentLength = body.length;
    req.add(body);
    final resp = await req.close();
    final text = await utf8.decodeStream(resp);
    if (resp.statusCode != 200) {
      return LlmResponse.failure(
        'Provider returned HTTP ${resp.statusCode}: ${_head(text, 200)}',
      );
    }
    final data = _decodeJson(resp, text);
    if (data is! Map<String, Object?>) {
      throw const FormatException('the reply is not a JSON object');
    }
    final choices = data['choices'];
    if (choices is! List || choices.isEmpty) {
      return LlmResponse.failure('Empty response from provider.');
    }
    final first = choices.first;
    final message = first is Map<String, Object?>
        ? (first['message'] as Map<String, Object?>? ?? <String, Object?>{})
        : <String, Object?>{};
    final content = message['content'];
    return LlmResponse(
      // Python keeps any truthy content; providers send a string or null.
      content: content is String ? content : '',
      toolCalls: parseToolCalls(message['tool_calls']),
      rawMessage: message,
    );
  }

  /// `_parse_tool_calls`: OpenAI-style tool_calls as `{id, name, args}`,
  /// arguments taken as a JSON string (Gemini, OpenAI) or an object (some
  /// local servers); anything else is `{}`. A call without an id gets
  /// `<name>_<index>`, so two parallel calls of one function stay apart.
  static List<ToolCall> parseToolCalls(Object? raw) {
    final out = <ToolCall>[];
    if (raw is! List) return out;
    for (var i = 0; i < raw.length; i++) {
      final tc = raw[i];
      if (tc is! Map<String, Object?>) continue;
      final fnRaw = tc['function'];
      final fn = fnRaw is Map<String, Object?> ? fnRaw : <String, Object?>{};
      final name = fn['name'];
      if (name is! String || name.isEmpty) continue;
      Object? args = fn.containsKey('arguments')
          ? fn['arguments']
          : <String, Object?>{};
      if (args is String) {
        try {
          args = pyStrip(args).isNotEmpty ? jsonDecode(args) : {};
        } catch (_) {
          args = <String, Object?>{};
        }
      }
      final id = tc['id'];
      out.add(
        ToolCall(
          id is String && id.isNotEmpty ? id : '${name}_$i',
          name,
          args is Map<String, Object?> ? args : <String, Object?>{},
        ),
      );
    }
    return out;
  }
}

/// The first [n] characters (code points), as Python's `text[:n]`.
String _head(String s, int n) {
  final r = s.runes;
  return r.length <= n ? s : String.fromCharCodes(r.take(n));
}

/// aiohttp's `resp.json()`: refuses a reply that isn't application/json.
Object? _decodeJson(HttpClientResponse resp, String text) {
  final mime = resp.headers.contentType?.mimeType ?? '';
  if (mime != 'application/json') {
    throw FormatException(
      'Attempt to decode JSON with unexpected mimetype: $mime',
    );
  }
  return jsonDecode(text);
}

/// A query value as aiohttp (yarl) encodes it: UTF-8, `+&=;` and
/// everything outside the unreserved, sub-delim and `?/:@` characters
/// percent-encoded, a space as `+`.
String _qsQuote(String s) =>
    pyQuote(s, safe: "?/:@!\$'()*, ").replaceAll(' ', '+');

/// A failure of [listGeminiModels]: Python's RuntimeError.
class LlmError implements Exception {
  const LlmError(this.message);
  final String message;
  @override
  String toString() => message;
}

/// One entry of [listGeminiModels].
typedef GeminiModel = ({String id, String name});

/// `list_gemini_models`: the chat-capable Gemini models this key can use,
/// newest id first. Google adds and retires models every few months, so
/// Settings fills its list from this. Throws [LlmError] with the
/// provider's reason (bad key, offline).
Future<List<GeminiModel>> listGeminiModels(
  String apiKey, {
  Duration timeout = const Duration(seconds: 15),
  String url = geminiModelsUrl,
}) async {
  final key = pyStrip(apiKey);
  if (key.isEmpty) throw const LlmError('No Gemini API key set.');
  final out = <GeminiModel>[];
  var pageToken = '';
  final http = HttpClient();
  try {
    for (var page = 0; page < 10; page++) {
      final query = pageToken.isEmpty
          ? 'pageSize=1000'
          : 'pageSize=1000&pageToken=${_qsQuote(pageToken)}';
      final uri = Uri.parse('$url?$query');
      final (status, text, mime) = await () async {
        final req = await http.getUrl(uri);
        req.headers.set('x-goog-api-key', key);
        final resp = await req.close();
        return (
          resp.statusCode,
          await utf8.decodeStream(resp),
          resp.headers.contentType?.mimeType ?? '',
        );
      }().timeout(timeout);
      if (status != 200) throw LlmError('HTTP $status: ${_head(text, 200)}');
      if (mime != 'application/json') {
        throw LlmError(
          'Attempt to decode JSON with unexpected mimetype: $mime',
        );
      }
      final data = jsonDecode(text) as Map<String, Object?>;
      for (final m in (data['models'] as List<Object?>?) ?? const []) {
        if (m is! Map<String, Object?>) continue;
        var id = '${m['name'] ?? ''}';
        if (id.startsWith('models/')) id = id.substring(7);
        final methods =
            (m['supportedGenerationMethods'] as List<Object?>?) ?? const [];
        if (!id.startsWith('gemini-') || !methods.contains('generateContent')) {
          continue;
        }
        if (_nonChatMarkers.any(id.contains)) continue;
        final display = m['displayName'];
        out.add((
          id: id,
          name: display is String && display.isNotEmpty ? display : id,
        ));
      }
      final next = data['nextPageToken'];
      pageToken = next is String ? next : '';
      if (pageToken.isEmpty) break;
    }
  } finally {
    http.close(force: true);
  }
  // sorted(key=id, reverse=True): stable, so equal ids keep their order.
  final idx = List.generate(out.length, (i) => i)
    ..sort((a, b) {
      final c = pyStrCompare(out[b].id, out[a].id);
      return c != 0 ? c : a - b;
    });
  return [for (final i in idx) out[i]];
}
