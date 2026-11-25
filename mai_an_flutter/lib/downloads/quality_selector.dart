import 'package:flutter/material.dart';

import '../ui/tokens.dart';
import 'download_queue.dart';

/// Port of `StreamripApp/ui/player/quality_selector.py`: single-item and batch
/// download quality picker that honours Settings → Storage → Default Download
/// Quality (`general.default_quality`) and skips the sheet when configured.

class QualityTierOption {
  const QualityTierOption({
    required this.tier,
    required this.title,
    required this.subtitle,
    required this.icon,
  });

  final String tier;
  final String title;
  final String subtitle;
  final IconData icon;
}

const kQualityTiers = <QualityTierOption>[
  QualityTierOption(
    tier: 'mp3',
    title: 'High',
    subtitle: 'MP3 / AAC 320kbps',
    icon: Icons.music_note,
  ),
  QualityTierOption(
    tier: 'cd',
    title: 'CD Quality',
    subtitle: '16-bit FLAC',
    icon: Icons.album,
  ),
  QualityTierOption(
    tier: 'hires',
    title: 'Hi-Res',
    subtitle: '24-bit FLAC',
    icon: Icons.high_quality,
  ),
];

const kQualityTierKeys = {'mp3', 'cd', 'hires'};

/// `quality_selector.hires_subtitle`: Deezer has no 24-bit tier, so its Hi-Res
/// downloads are 16-bit FLAC; the sheet must not promise otherwise.
String hiresSubtitle(List<Map<String, Object?>> records) {
  final sources = {
    for (final r in records)
      ('${r['source'] ?? 'qobuz'}'.isEmpty ? 'qobuz' : '${r['source']}')
          .toLowerCase(),
  };
  if (sources.length == 1 && sources.contains('deezer')) {
    return "16-bit FLAC (Deezer's best)";
  }
  if (sources.contains('deezer')) {
    return '24-bit FLAC (Deezer items: 16-bit)';
  }
  return '24-bit FLAC';
}

/// Normalise `general.default_quality` into a valid tier key or null ("Ask").
String? normalizedDefaultTier(String? raw) {
  final t = (raw ?? '').trim().toLowerCase();
  return kQualityTierKeys.contains(t) ? t : null;
}

/// Present the quality selector for one item (or enqueue immediately when a
/// default tier is configured in Settings).
Future<String?> showQualitySelector(
  BuildContext context, {
  required Map<String, Object?> record,
  required DownloadQueue queue,
  required void Function(String message, {IconData? icon}) onSnack,
  String? defaultQuality,
  VoidCallback? onDone,
}) => showQualitySelectorBatch(
  context,
  records: [record],
  queue: queue,
  onSnack: onSnack,
  defaultQuality: defaultQuality,
  onDone: onDone,
);

/// Present the quality selector for a batch of items at one quality tier.
/// Returns the selected tier key, or null if dismissed.
Future<String?> showQualitySelectorBatch(
  BuildContext context, {
  required List<Map<String, Object?>> records,
  required DownloadQueue queue,
  required void Function(String message, {IconData? icon}) onSnack,
  String? defaultQuality,
  VoidCallback? onDone,
}) async {
  if (records.isEmpty) return null;

  final preset = normalizedDefaultTier(defaultQuality);
  if (preset != null) {
    _submitRecords(records, preset, queue, onSnack, onDone);
    return preset;
  }

  final subheading = records.length > 1
      ? '${records.length} items'
      : '${records.first['ui_title'] ?? records.first['name'] ?? ''}';
  final hiResSub = hiresSubtitle(records);

  final picked = await showModalBottomSheet<String>(
    context: context,
    backgroundColor: Tokens.surface,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(18)),
    ),
    builder: (ctx) {
      final accent = Theme.of(ctx).colorScheme.primary;
      return SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                'Select Quality',
                style: TextStyle(
                  color: Tokens.text,
                  fontSize: 15,
                  fontWeight: FontWeight.w700,
                ),
              ),
              if (subheading.isNotEmpty) ...[
                const SizedBox(height: 2),
                Text(
                  subheading,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(color: Tokens.dim, fontSize: 12),
                ),
              ],
              const Divider(color: Tokens.border),
              for (final opt in kQualityTiers)
                ListTile(
                  leading: Icon(opt.icon, color: accent),
                  title: Text(
                    opt.title,
                    style: const TextStyle(color: Tokens.text),
                  ),
                  subtitle: Text(
                    opt.tier == 'hires' ? hiResSub : opt.subtitle,
                    style: const TextStyle(color: Tokens.dim),
                  ),
                  onTap: () => Navigator.of(ctx).pop(opt.tier),
                ),
            ],
          ),
        ),
      );
    },
  );

  if (picked == null) return null;
  _submitRecords(records, picked, queue, onSnack, onDone);
  return picked;
}

void _submitRecords(
  List<Map<String, Object?>> records,
  String tier,
  DownloadQueue queue,
  void Function(String message, {IconData? icon}) onSnack,
  VoidCallback? onDone,
) {
  final String message;
  if (records.length == 1) {
    final item = records.first;
    queue.enqueue(item, qualityTier: tier);
    final label = '${item['ui_title'] ?? item['name'] ?? 'item'}';
    message = 'Added “$label” to downloads (${tier.toUpperCase()}).';
  } else {
    queue.enqueueMany(records, qualityTier: tier);
    message =
        'Added ${records.length} items to downloads (${tier.toUpperCase()}).';
  }
  onSnack(message, icon: Icons.download_rounded);
  onDone?.call();
}
