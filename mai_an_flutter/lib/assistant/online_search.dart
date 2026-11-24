import 'dart:async';

import '../core/py_values.dart';
import '../sources/source_config.dart';
import '../sources/streamrip_api.dart';
import '../sources/streamrip_search.dart';
import '../sources/tagger.dart';
import 'runner.dart';
import 'tool_executor.dart';

/// Port of `StreamripApp/utils/llm_tools.py`'s `search_online`, `_search_qobuz`,
/// and `_download_item` hooks into `AssistantRunner.onlineSearchHandler`.

typedef OnlineDownloadTrigger =
    Future<void> Function(Map<String, Object?> item);

/// Builds the `search_online` [ToolHandler] for [AssistantRunner].
ToolHandler buildOnlineSearchHandler({
  required StreamripSearcher searcher,
  Future<SourceConfig> Function()? readConfig,
  Future<String> Function()? downloadDir,
  bool Function(String path)? isAppPrivate,
  Future<void> Function(String path)? onFileSaved,
  OnlineDownloadTrigger? onDownloadItem,
  Duration searchTimeout = const Duration(seconds: 25),
}) {
  return (String toolName, Map<String, Object?> args, AssistantRunner runner) async {
    final query = '${args['query'] ?? ''}'.trim();
    final wantDownload = pyTruthy(args['download']);
    if (query.isEmpty) {
      return {'success': false, 'error': 'Empty online query.'};
    }

    final ({List<Map<String, Object?>> items, String? error}) res;
    try {
      res = await searcher
          .search(
            query,
            source: 'qobuz',
            mediaTypes: const ['track', 'album'],
            limit: 10,
          )
          .timeout(searchTimeout);
    } on TimeoutException {
      return {'success': false, 'error': 'Online search timed out.'};
    } catch (e) {
      return {'success': false, 'error': '$e'};
    }

    if (res.error != null) {
      return {'success': false, 'error': res.error};
    }
    if (res.items.isEmpty) {
      return {
        'success': true,
        'results': <Object?>[],
        'message': "No online results for '$query'.",
      };
    }

    final slim = <Map<String, Object?>>[
      for (final r in res.items.take(5))
        {
          'title': r['name'],
          'artist': r['artist'],
          'type': r['media_type'],
          'url': r['url'],
        },
    ];

    if (wantDownload && slim.isNotEmpty) {
      final top = slim.first;
      return runner.agentRequestConfirmation(
        spoken:
            "I found '${top['title']}' by ${top['artist']} on Qobuz, sir. Shall I download it?",
        displayed:
            'Found **${top['title']}** — ${top['artist']} on Qobuz. Download it?',
        onYes: () => _executeDownloadItem(
          top,
          readConfig: readConfig ?? searcher.readConfig,
          downloadDir: downloadDir,
          isAppPrivate: isAppPrivate,
          onFileSaved: onFileSaved,
          onDownloadItem: onDownloadItem,
        ),
      );
    }

    return {'success': true, 'results': slim};
  };
}

Future<AssistantResponse> _executeDownloadItem(
  Map<String, Object?> item, {
  required Future<SourceConfig> Function() readConfig,
  Future<String> Function()? downloadDir,
  bool Function(String path)? isAppPrivate,
  Future<void> Function(String path)? onFileSaved,
  OnlineDownloadTrigger? onDownloadItem,
}) async {
  final url = '${item['url'] ?? ''}'.trim();
  if (url.isEmpty) {
    return AssistantResponse(
      spoken: "I couldn't resolve that item's download link, sir.",
      displayed: 'Download failed: missing item URL.',
      success: false,
    );
  }
  try {
    if (onDownloadItem != null) {
      await onDownloadItem(item);
    } else {
      final dir = await downloadDir?.call();
      if (dir == null || dir.isEmpty) {
        throw StateError('No download directory configured.');
      }
      final cfg = await readConfig();
      await downloadFromSource(
        url: url,
        downloadDir: dir,
        config: cfg,
        isAppPrivate: isAppPrivate,
        tagWriter: tagFile,
        onFileSaved: onFileSaved,
      );
    }
    return AssistantResponse(
      spoken:
          "Downloading '${item['title']}' by ${item['artist']} now, sir.",
      displayed: 'Started download: **${item['title']}** — ${item['artist']}.',
    );
  } catch (e) {
    return AssistantResponse(
      spoken: 'The download could not be started, sir.',
      displayed: 'Download error: $e',
      success: false,
    );
  }
}
