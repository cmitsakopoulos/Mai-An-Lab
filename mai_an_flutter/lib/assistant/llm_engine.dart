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

  /// [chatCompletion] with `stream: true`: reply text reaches [onDelta] as
  /// it is generated, and the assembled reply is returned as usual (tool
  /// calls and the provider's raw message included, so a Gemini
  /// thought_signature survives). A server that answers with plain JSON is
  /// read as such; one that refuses streaming before sending anything gets
  /// one non-streaming retry. Between chunks the limit is [streamIdle]
  /// rather than a total, so a long answer isn't cut off.
  Future<LlmResponse> chatCompletionStream(
    List<Map<String, Object?>> messages, {
    List<Map<String, Object?>>? tools,
    required void Function(String delta) onDelta,
  }) async {
    if (!isConfigured) return chatCompletion(messages, tools: tools);
    final gemini = provider == 'gemini';
    try {
      return await _streamOpenAiCompatible(
        gemini ? geminiUrl : '$ollamaEndpoint/chat/completions',
        {
          if (gemini) 'Authorization': 'Bearer ${pyStrip(apiKey)}',
          'Content-Type': 'application/json',
        },
        gemini ? model : ollamaModel,
        messages,
        tools,
        firstByte: timeoutOverride ?? Duration(seconds: gemini ? 30 : 45),
        idle: timeoutOverride ?? streamIdle,
        onDelta: onDelta,
      );
    } on _StreamUnsupported {
      return chatCompletion(messages, tools: tools);
    }
  }

  /// The longest gap between streamed chunks before the turn gives up.
  static const streamIdle = Duration(seconds: 20);

  static List<Map<String, Object?>>? _wrapTools(
    List<Map<String, Object?>>? tools,
  ) => tools == null || tools.isEmpty
      ? null
      : [
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

  Future<LlmResponse> _streamOpenAiCompatible(
    String url,
    Map<String, String> headers,
    String model,
    List<Map<String, Object?>> messages,
    List<Map<String, Object?>>? tools, {
    required Duration firstByte,
    required Duration idle,
    required void Function(String delta) onDelta,
  }) async {
    final payload = <String, Object?>{
      'model': model,
      'messages': messages,
      'stream': true,
    };
    final wrapped = _wrapTools(tools);
    if (wrapped != null) payload['tools'] = wrapped;
    final body = utf8.encode(pyJsonDumps(payload));

    final acc = StreamAccumulator();
    for (var attempt = 1; ; attempt++) {
      final http = HttpClient()
        ..connectionTimeout = const Duration(seconds: 10);
      try {
        final HttpClientResponse resp;
        try {
          final req = await http.postUrl(Uri.parse(url)).timeout(firstByte);
          headers.forEach(req.headers.set);
          req.contentLength = body.length;
          req.add(body);
          resp = await req.close().timeout(firstByte);
        } on SocketException catch (e) {
          if (attempt == 1 && !e.message.contains('timed out')) {
            await Future<void>.delayed(retryDelay);
            continue;
          }
          return LlmResponse.failure('Network error: $e');
        }

        if (resp.statusCode != 200) {
          final text = await utf8.decodeStream(resp).timeout(idle);
          if (_noStreamStatuses.contains(resp.statusCode)) {
            throw const _StreamUnsupported();
          }
          return LlmResponse.failure(
            'Provider returned HTTP ${resp.statusCode}: ${_head(text, 200)}',
          );
        }
        final mime = resp.headers.contentType?.mimeType ?? '';
        if (mime == 'application/json') {
          // The server ignored `stream`; read the whole reply.
          final text = await utf8.decodeStream(resp).timeout(firstByte);
          final r = _parseCompletion(_decodeJson(resp, text));
          if (r.success && r.content.isNotEmpty) onDelta(r.content);
          return r;
        }

        final lines = resp
            .cast<List<int>>()
            .transform(utf8.decoder)
            .transform(const LineSplitter())
            .timeout(idle);
        await for (final line in lines) {
          if (!line.startsWith('data:')) continue;
          final data = line.substring(5).trim();
          if (data.isEmpty) continue;
          if (data == '[DONE]') break;
          final Object? chunk;
          try {
            chunk = jsonDecode(data);
          } on FormatException {
            if (!acc.started) throw const _StreamUnsupported();
            rethrow;
          }
          final err = acc.add(chunk);
          if (err != null) return LlmResponse.failure(err);
          final d = acc.takeDelta();
          if (d.isNotEmpty) onDelta(d);
        }
        if (!acc.started) throw const _StreamUnsupported();
        return acc.result();
      } on TimeoutException {
        return LlmResponse.failure('Network error: ');
      } on _StreamUnsupported {
        rethrow;
      } catch (e) {
        return LlmResponse.failure('Network error: $e');
      } finally {
        http.close(force: true);
      }
    }
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
    return _parseCompletion(_decodeJson(resp, text));
  }

  LlmResponse _parseCompletion(Object? data) {
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

/// Statuses that mean "this server won't stream", answered by one
/// non-streaming retry.
const _noStreamStatuses = {400, 404, 405, 415, 422, 501};

class _StreamUnsupported implements Exception {
  const _StreamUnsupported();
}

/// Assembles OpenAI-style `chat.completion.chunk` deltas into the message a
/// non-streaming call would have returned. Text fields are concatenated;
/// tool calls are grouped by `index` (or by a new `id` when a provider
/// omits the index) with their `arguments` joined; every other field,
/// Gemini's `extra_content.google.thought_signature` included, is merged
/// through unchanged so the message can be echoed back on the next turn.
class StreamAccumulator {
  final Map<String, Object?> _msg = {};
  final StringBuffer _content = StringBuffer();
  final List<Map<String, Object?>> _calls = [];
  final StringBuffer _pending = StringBuffer();
  bool _started = false;

  /// True once any chunk with choices arrived.
  bool get started => _started;

  static const _concatKeys = {'content', 'reasoning_content', 'reasoning'};

  /// Folds in one decoded chunk; returns an error message for an error
  /// chunk, else null.
  String? add(Object? chunk) {
    if (chunk is! Map<String, Object?>) return null;
    final error = chunk['error'];
    if (error != null) {
      final m = error is Map<String, Object?> ? error['message'] : error;
      return 'Provider error: ${m ?? error}';
    }
    final choices = chunk['choices'];
    if (choices is! List || choices.isEmpty) return null;
    _started = true;
    final first = choices.first;
    if (first is! Map<String, Object?>) return null;
    final delta = first['delta'] ?? first['message'];
    if (delta is! Map<String, Object?>) return null;
    for (final MapEntry(:key, :value) in delta.entries) {
      if (key == 'tool_calls') {
        if (value is List) {
          for (final tc in value) {
            if (tc is Map<String, Object?>) _addCall(tc);
          }
        }
      } else if (key == 'content') {
        if (value is String && value.isNotEmpty) {
          _content.write(value);
          _pending.write(value);
        }
      } else if (_concatKeys.contains(key) && value is String) {
        final prev = _msg[key];
        _msg[key] = prev is String ? '$prev$value' : value;
      } else if (value != null) {
        _msg[key] = _merge(_msg[key], value);
      }
    }
    return null;
  }

  void _addCall(Map<String, Object?> tc) {
    final index = tc['index'];
    final id = tc['id'];
    Map<String, Object?>? slot;
    if (index is int) {
      while (_calls.length <= index) {
        _calls.add({});
      }
      slot = _calls[index];
    } else if (id is String &&
        id.isNotEmpty &&
        !_calls.any((c) => c['id'] == id)) {
      slot = {};
      _calls.add(slot);
    } else if (_calls.isNotEmpty) {
      slot = _calls.last;
    } else {
      slot = {};
      _calls.add(slot);
    }
    for (final MapEntry(:key, :value) in tc.entries) {
      if (key == 'index' || value == null) continue;
      if (key == 'function' && value is Map<String, Object?>) {
        final fn = (slot['function'] as Map<String, Object?>?) ?? {};
        for (final MapEntry(key: k, value: v) in value.entries) {
          if (k == 'arguments' && v is String) {
            final prev = fn['arguments'];
            fn['arguments'] = prev is String ? '$prev$v' : v;
          } else if (v != null) {
            fn[k] = _merge(fn[k], v);
          }
        }
        slot['function'] = fn;
      } else {
        slot[key] = _merge(slot[key], value);
      }
    }
  }

  static Object? _merge(Object? prev, Object? next) {
    if (prev is Map<String, Object?> && next is Map<String, Object?>) {
      final out = Map<String, Object?>.of(prev);
      next.forEach((k, v) => out[k] = _merge(out[k], v));
      return out;
    }
    return next;
  }

  /// Text that arrived since the last call.
  String takeDelta() {
    final d = _pending.toString();
    _pending.clear();
    return d;
  }

  /// The assembled reply.
  LlmResponse result() {
    final content = _content.toString();
    final calls = [
      for (final c in _calls)
        if (c.isNotEmpty) c,
    ];
    final raw = <String, Object?>{
      'role': 'assistant',
      ..._msg,
      'content': content.isEmpty && calls.isNotEmpty ? null : content,
      if (calls.isNotEmpty) 'tool_calls': calls,
    };
    return LlmResponse(
      content: content,
      toolCalls: LlmEngine.parseToolCalls(calls),
      rawMessage: raw,
    );
  }
}
