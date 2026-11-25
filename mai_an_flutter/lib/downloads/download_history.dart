import 'dart:convert';
import 'dart:io';

/// Port of `StreamripApp/utils/history_manager.py`: persists up to 100 recent
/// download records in `<dataDir>/history.json` (deduplicated by `url`, newest
/// first, 4-space indented JSON).
class DownloadHistoryStore {
  DownloadHistoryStore(this.dataDir, {DateTime Function()? clock})
    : _clock = clock ?? DateTime.now;

  static const maxEntries = 100;

  final String dataDir;
  final DateTime Function() _clock;

  File get file => File('$dataDir${Platform.pathSeparator}history.json');

  /// `history_manager.load_history`.
  Future<List<Map<String, Object?>>> load() async {
    final f = file;
    if (!await f.exists()) return const [];
    try {
      final decoded = jsonDecode(await f.readAsString());
      if (decoded is! List) return const [];
      return [
        for (final item in decoded)
          if (item is Map) {for (final e in item.entries) '${e.key}': e.value},
      ];
    } catch (_) {
      return const [];
    }
  }

  /// `history_manager.save_history`.
  Future<void> save(List<Map<String, Object?>> history) async {
    final f = file;
    await f.parent.create(recursive: true);
    const encoder = JsonEncoder.withIndent('    ');
    await f.writeAsString(encoder.convert(history));
  }

  /// `history_manager.add_to_history`.
  Future<List<Map<String, Object?>>> add(Map<String, Object?> itemData) async {
    final now = _clock();
    String two(int n) => n.toString().padLeft(2, '0');
    final date =
        '${now.year}-${two(now.month)}-${two(now.day)} '
        '${two(now.hour)}:${two(now.minute)}';

    final entry = <String, Object?>{
      'name': '${itemData['name'] ?? itemData['ui_title'] ?? 'Unknown'}',
      'artist':
          '${itemData['artist'] ?? itemData['ui_subtitle'] ?? 'Unknown Artist'}',
      'source': '${itemData['source'] ?? 'qobuz'}',
      'image': '${itemData['image'] ?? itemData['image_url'] ?? ''}',
      'year': '${itemData['year'] ?? 'N/A'}',
      'media_type': '${itemData['media_type'] ?? 'track'}',
      'url': '${itemData['url'] ?? ''}',
      'date': date,
    };

    final existing = await load();
    final updated = <Map<String, Object?>>[
      entry,
      for (final h in existing)
        if ('${h['url'] ?? ''}' != entry['url']) h,
    ];
    final trimmed = updated.length > maxEntries
        ? updated.sublist(0, maxEntries)
        : updated;
    await save(trimmed);
    return trimmed;
  }
}
