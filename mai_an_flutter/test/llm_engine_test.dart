import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:mai_an_lab/assistant/agent_tools.dart';
import 'package:mai_an_lab/assistant/llm_engine.dart';
import 'package:mai_an_lab/core/py_json.dart';

/// Phase 5 stage b against what Flet's `llm_engine` sent and made of the
/// same replies (`tool/export_llm_goldens.py`).
void main() {
  final golden =
      jsonDecode(File('test/goldens/llm_engine.json').readAsStringSync())
          as Map<String, Object?>;

  late HttpServer server;
  late String base;
  final replies = <Map<String, Object?>>[];
  final requests = <Map<String, Object?>>[];

  setUp(() async {
    replies.clear();
    requests.clear();
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    base = 'http://127.0.0.1:${server.port}';
    server.listen((req) async {
      final body = await utf8.decodeStream(req);
      requests.add({
        'method': req.method,
        'path': req.uri.toString(),
        'headers': {
          for (final k in ['authorization', 'content-type', 'x-goog-api-key'])
            if (req.headers.value(k) != null) k: req.headers.value(k),
        },
        'body': body,
      });
      final r = replies.removeAt(0);
      req.response
        ..statusCode = r['status']! as int
        ..headers.set('content-type', r['content_type']! as String)
        ..add(utf8.encode(r['body']! as String));
      await req.response.close();
    });
  });
  tearDown(() => server.close(force: true));

  test('agentTools is Python\'s get_agent_tools, byte for byte', () {
    expect(pyJsonDumps(agentTools()), golden['tools_json']);
    expect(agentTools().length, 30);
  });

  for (final sc
      in (golden['chat']! as List<Object?>).cast<Map<String, Object?>>()) {
    test('chatCompletion: ${sc['name']}', () async {
      replies.addAll(
        (sc['replies']! as List<Object?>).cast<Map<String, Object?>>(),
      );
      final e = sc['engine']! as Map<String, Object?>;
      final engine = LlmEngine(
        provider: e['provider'] as String? ?? 'gemini',
        apiKey: e['api_key'] as String? ?? '',
        model: e['model'] as String? ?? defaultGeminiModel,
        ollamaEndpoint: e.containsKey('ollama_endpoint')
            ? '$base${e['ollama_endpoint']}'
            : defaultOllamaEndpoint,
        ollamaModel: e['ollama_model'] as String? ?? defaultOllamaModel,
        geminiUrl: '$base/v1beta/openai/chat/completions',
      );
      final tools = switch (sc['tools']) {
        true => agentTools(),
        'empty' => <Map<String, Object?>>[],
        _ => null,
      };
      final messages = (sc['messages']! as List<Object?>)
          .cast<Map<String, Object?>>();
      final r = await engine.chatCompletion(messages, tools: tools);
      final want = sc['result']! as Map<String, Object?>;
      expect(requests, sc['requests'], reason: 'requests');
      expect(r.success, want['success']);
      if (sc['network_error'] == true) {
        expect(r.errorMessage, startsWith('Network error: '));
        return;
      }
      expect(r.errorMessage, want['error_message']);
      expect(r.content, want['content']);
      expect(
        jsonEncode([for (final t in r.toolCalls) t.toJson()]),
        jsonEncode(want['tool_calls']),
      );
      expect(jsonEncode(r.rawMessage), jsonEncode(want['raw_message']));
    });
  }

  for (final sc
      in (golden['models']! as List<Object?>).cast<Map<String, Object?>>()) {
    test('listGeminiModels: ${sc['name']}', () async {
      replies.addAll(
        (sc['replies']! as List<Object?>).cast<Map<String, Object?>>(),
      );
      Object result;
      try {
        final models = await listGeminiModels(
          sc['api_key']! as String,
          url: '$base/v1beta/models',
        );
        result = {
          'models': [
            for (final m in models) {'id': m.id, 'name': m.name},
          ],
        };
      } on LlmError catch (e) {
        result = {'error': e.message};
      }
      expect(requests, sc['requests'], reason: 'requests');
      expect(result, sc['result']);
    });
  }

  test('a refused connection is retried once, then reported', () async {
    final closed = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
    final port = closed.port;
    await closed.close();
    final engine = LlmEngine(
      provider: 'ollama',
      ollamaEndpoint: 'http://127.0.0.1:$port/v1',
      retryDelay: const Duration(milliseconds: 50),
    );
    final sw = Stopwatch()..start();
    final r = await engine.chatCompletion([
      {'role': 'user', 'content': 'hi'},
    ]);
    expect(r.success, isFalse);
    expect(r.errorMessage, startsWith('Network error: '));
    expect(sw.elapsedMilliseconds, greaterThanOrEqualTo(50));
  });

  test('a stalled provider times out with Python\'s empty message', () async {
    final stall = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    stall.listen((_) {}); // never answers
    addTearDown(() => stall.close(force: true));
    final engine = LlmEngine(
      provider: 'ollama',
      ollamaEndpoint: 'http://127.0.0.1:${stall.port}/v1',
      timeoutOverride: const Duration(milliseconds: 300),
    );
    final r = await engine
        .chatCompletion([
          {'role': 'user', 'content': 'hi'},
        ])
        .timeout(const Duration(seconds: 5));
    expect(r.errorMessage, 'Network error: ');
  });

  group('chatCompletionStream', () {
    // A string starting with ':' is an SSE comment line, sent as is.
    String sse(List<Object> chunks) => [
      for (final c in chunks)
        c is String && c.startsWith(':')
            ? '$c\n\n'
            : 'data: ${c is String ? c : jsonEncode(c)}\n\n',
    ].join();
    Map<String, Object?> chunk(Map<String, Object?> delta) => {
      'choices': [
        {'index': 0, 'delta': delta},
      ],
    };
    LlmEngine gemini() => LlmEngine(
      apiKey: 'k',
      geminiUrl: '$base/v1beta/openai/chat/completions',
    );
    final msgs = [
      {'role': 'user', 'content': 'hi'},
    ];

    test(
      'text deltas arrive in order and the request asks to stream',
      () async {
        replies.add({
          'status': 200,
          'content_type': 'text/event-stream',
          'body': sse([
            chunk({'role': 'assistant', 'content': 'Hel'}),
            chunk({'content': 'lo, '}),
            ': keep-alive comment',
            chunk({'content': 'sir.'}),
            '[DONE]',
          ]),
        });
        final deltas = <String>[];
        final r = await gemini().chatCompletionStream(
          msgs,
          onDelta: deltas.add,
        );
        expect(r.success, isTrue);
        expect(r.content, 'Hello, sir.');
        expect(deltas, ['Hel', 'lo, ', 'sir.']);
        expect(r.toolCalls, isEmpty);
        expect(r.rawMessage, {'role': 'assistant', 'content': 'Hello, sir.'});
        final body = jsonDecode(requests.single['body']! as String) as Map;
        expect(body['stream'], isTrue);
        expect(
          (requests.single['headers']! as Map)['authorization'],
          'Bearer k',
        );
      },
    );

    test(
      'split tool-call arguments join; thought_signature survives',
      () async {
        replies.add({
          'status': 200,
          'content_type': 'text/event-stream',
          'body': sse([
            chunk({
              'role': 'assistant',
              'tool_calls': [
                {
                  'index': 0,
                  'id': 'c1',
                  'type': 'function',
                  'function': {'name': 'search_library', 'arguments': '{"que'},
                  'extra_content': {
                    'google': {'thought_signature': 'SIG=='},
                  },
                },
              ],
            }),
            chunk({
              'tool_calls': [
                {
                  'index': 0,
                  'function': {'arguments': 'ry": "Ride"}'},
                },
                {
                  'index': 1,
                  'id': 'c2',
                  'type': 'function',
                  'function': {'name': 'list_artists', 'arguments': '{}'},
                },
              ],
            }),
            '[DONE]',
          ]),
        });
        final deltas = <String>[];
        final r = await gemini().chatCompletionStream(
          msgs,
          onDelta: deltas.add,
        );
        expect(deltas, isEmpty);
        expect(
          [for (final c in r.toolCalls) c.toJson()],
          [
            {
              'id': 'c1',
              'name': 'search_library',
              'args': {'query': 'Ride'},
            },
            {'id': 'c2', 'name': 'list_artists', 'args': <String, Object?>{}},
          ],
        );
        expect(r.rawMessage, {
          'role': 'assistant',
          'content': null,
          'tool_calls': [
            {
              'id': 'c1',
              'type': 'function',
              'function': {
                'name': 'search_library',
                'arguments': '{"query": "Ride"}',
              },
              'extra_content': {
                'google': {'thought_signature': 'SIG=='},
              },
            },
            {
              'id': 'c2',
              'type': 'function',
              'function': {'name': 'list_artists', 'arguments': '{}'},
            },
          ],
        });
      },
    );

    test('tool calls without an index are told apart by id', () async {
      replies.add({
        'status': 200,
        'content_type': 'text/event-stream',
        'body': sse([
          chunk({
            'tool_calls': [
              {
                'id': 'a',
                'function': {'name': 'list_artists', 'arguments': '{}'},
              },
            ],
          }),
          chunk({
            'tool_calls': [
              {
                'id': 'b',
                'function': {'name': 'list_playlists', 'arguments': '{}'},
              },
            ],
          }),
        ]),
      });
      final r = await gemini().chatCompletionStream(msgs, onDelta: (_) {});
      expect([for (final c in r.toolCalls) c.id], ['a', 'b']);
    });

    test('a plain JSON reply to a stream request is read whole', () async {
      replies.add({
        'status': 200,
        'content_type': 'application/json',
        'body': jsonEncode({
          'choices': [
            {
              'message': {'role': 'assistant', 'content': 'All at once.'},
            },
          ],
        }),
      });
      final deltas = <String>[];
      final r = await gemini().chatCompletionStream(msgs, onDelta: deltas.add);
      expect(r.content, 'All at once.');
      expect(deltas, ['All at once.']);
    });

    test('a server that refuses streaming gets one plain retry', () async {
      replies.addAll([
        {
          'status': 400,
          'content_type': 'application/json',
          'body': '{"error": "stream not supported"}',
        },
        {
          'status': 200,
          'content_type': 'application/json',
          'body': jsonEncode({
            'choices': [
              {
                'message': {'role': 'assistant', 'content': 'Fine.'},
              },
            ],
          }),
        },
      ]);
      final r = await gemini().chatCompletionStream(msgs, onDelta: (_) {});
      expect(r.content, 'Fine.');
      expect(requests, hasLength(2));
      final second = jsonDecode(requests[1]['body']! as String) as Map;
      expect(second.containsKey('stream'), isFalse);
    });

    test('an auth failure is reported, not retried', () async {
      replies.add({
        'status': 401,
        'content_type': 'application/json',
        'body': '{"error": "bad key"}',
      });
      final r = await gemini().chatCompletionStream(msgs, onDelta: (_) {});
      expect(r.success, isFalse);
      expect(r.errorMessage, startsWith('Provider returned HTTP 401'));
      expect(requests, hasLength(1));
    });

    test('an error chunk mid-stream fails the reply', () async {
      replies.add({
        'status': 200,
        'content_type': 'text/event-stream',
        'body': sse([
          chunk({'content': 'Part'}),
          {
            'error': {'message': 'quota exceeded'},
          },
        ]),
      });
      final r = await gemini().chatCompletionStream(msgs, onDelta: (_) {});
      expect(r.success, isFalse);
      expect(r.errorMessage, 'Provider error: quota exceeded');
    });

    test('a stream that stalls between chunks times out', () async {
      final stall = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
      addTearDown(() => stall.close(force: true));
      stall.listen((req) async {
        await utf8.decodeStream(req);
        req.response
          ..headers.set('content-type', 'text/event-stream')
          ..bufferOutput = false
          ..write(
            sse([
              chunk({'content': 'Start'}),
            ]),
          );
        await req.response.flush();
        // ...and never another byte.
      });
      final engine = LlmEngine(
        provider: 'ollama',
        ollamaEndpoint: 'http://127.0.0.1:${stall.port}/v1',
        timeoutOverride: const Duration(milliseconds: 300),
      );
      final deltas = <String>[];
      final r = await engine
          .chatCompletionStream(msgs, onDelta: deltas.add)
          .timeout(const Duration(seconds: 5));
      expect(deltas, ['Start']);
      expect(r.errorMessage, 'Network error: ');
    });
  });

  test('isConfigured and the defaults', () {
    expect(LlmEngine(apiKey: ' ').isConfigured, isFalse);
    expect(LlmEngine(apiKey: 'k').isConfigured, isTrue);
    expect(
      LlmEngine(provider: 'ollama', ollamaEndpoint: '').isConfigured,
      isFalse,
    );
    expect(LlmEngine(provider: 'ollama').isConfigured, isTrue);
    expect(LlmEngine(model: '').model, defaultGeminiModel);
    expect(
      LlmEngine(ollamaEndpoint: 'http://h/v1///').ollamaEndpoint,
      'http://h/v1',
    );
  });
}
