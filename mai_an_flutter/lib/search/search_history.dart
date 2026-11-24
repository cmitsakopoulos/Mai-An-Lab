import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

/// Port of `StreamripApp/utils/search_history.py`: stores up to 10 most recent
/// search queries in `<dataDir>/recent_searches.json`.
class SearchHistoryStore {
  SearchHistoryStore(this.dataDir);

  final String dataDir;

  static const maxEntries = 10;

  File get file => File('$dataDir${Platform.pathSeparator}recent_searches.json');

  /// `load_searches()`: returns the saved list, or empty when missing/corrupt.
  Future<List<String>> load() async => loadSync();

  /// Synchronous read matching Python's `search_history.load_searches` (at most
  /// 10 short query strings, < 1 KB).
  List<String> loadSync() {
    try {
      final decoded = jsonDecode(file.readAsStringSync());
      if (decoded is! List) return const [];
      return [
        for (final item in decoded)
          if (item is String) item,
      ];
    } catch (_) {
      return const [];
    }
  }

  /// `add_search(query)`: deduplicates, moves [query] to the top, caps at 10,
  /// and writes indented JSON matching Python's `json.dump(..., indent=4)`.
  Future<List<String>> add(String query) async {
    if (query.isEmpty) return load();
    final searches = (await load()).toList();
    searches.remove(query);
    searches.insert(0, query);
    final trimmed = searches.length > maxEntries
        ? searches.sublist(0, maxEntries)
        : searches;
    try {
      await file.parent.create(recursive: true);
      const encoder = JsonEncoder.withIndent('    ');
      await file.writeAsString(encoder.convert(trimmed));
    } catch (e) {
      debugPrint('Failed to save searches: $e');
    }
    return trimmed;
  }
}
