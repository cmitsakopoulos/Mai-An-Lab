import 'dart:math' as math;

import 'package:flutter/material.dart';

/// settings.py `SettingSearchEntry`: one hub row as the search box sees it.
class SettingEntry {
  const SettingEntry({
    required this.page,
    required this.title,
    required this.subtitle,
    required this.category,
    required this.icon,
    required this.keywords,
  });

  final SettingsPage page;
  final String title, subtitle, category;
  final IconData icon;
  final List<String> keywords;
}

/// The hub's groups the port has (settings.py `open_subpage` names). Those
/// waiting on later phases (Authentication, AI Assistant, Metadata) are
/// left out of the hub and the search alike.
enum SettingsPage {
  companion('Companion & Sync'),
  storage('Storage'),
  appearance('Appearance'),
  audio('Audio & DSP'),
  haptics('Haptic Feedback'),
  permissions('Permissions'),
  database('Database Management'),
  advanced('Advanced'),
  about('About');

  const SettingsPage(this.title);

  /// The sub-page's header (Flet's `_show_sub_page` title).
  final String title;
}

/// settings.py `_build_search_registry`, the ported entries, in hub order.
const kSettingEntries = [
  SettingEntry(
    page: SettingsPage.companion,
    title: 'Companion & Sync',
    subtitle: 'Wireless feature offload and device transfers',
    category: 'GENERAL',
    icon: Icons.sync_rounded,
    keywords: [
      'companion', 'sync', 'offload', 'dsp', 'mac', 'android', 'wireless', //
      'bluetooth', 'p2p', 'remote', 'pairing', 'pin', 'server',
    ],
  ),
  SettingEntry(
    page: SettingsPage.storage,
    title: 'Storage & Paths',
    subtitle: 'Library and download locations',
    category: 'GENERAL',
    icon: Icons.storage_rounded,
    keywords: [
      'download', 'path', 'folder', 'directory', 'streamrip', 'location', //
      'storage', 'music folder', 'save', 'destination', 'library path',
      'db path', 'disk', 'target',
    ],
  ),
  SettingEntry(
    page: SettingsPage.appearance,
    title: 'Appearance',
    subtitle: 'Accent colors and UI behavior',
    category: 'CUSTOMISATION',
    icon: Icons.palette_rounded,
    keywords: [
      'accent', 'color', 'theme', 'dark', 'light', 'visual', 'ui', //
      'appearance', 'font', 'startup', 'sort', 'landing', 'default sort',
      'history', 'stats', 'display', 'style', 'pill', 'pills', 'tabs', 'glow',
      'most played', 'most listened', 'top artists', 'top genres',
    ],
  ),
  SettingEntry(
    page: SettingsPage.audio,
    title: 'Audio & DSP',
    subtitle: 'Equalizer, presets & dynamism enhancement',
    category: 'CUSTOMISATION',
    icon: Icons.graphic_eq_rounded,
    keywords: [
      'eq', 'equalizer', 'bands', 'gain', 'dynamism', 'preset', 'acoustic', //
      'flat', 'bass', 'treble', 'dsp', 'loudness', 'boost', 'audio',
      'frequency', 'sound', 'volume', 'curve', 'enhancement', 'headroom',
      'preamp', 'decibel', 'db',
    ],
  ),
  SettingEntry(
    page: SettingsPage.haptics,
    title: 'Haptic Feedback',
    subtitle: 'Vibration settings and intensity controls',
    category: 'CUSTOMISATION',
    icon: Icons.vibration_rounded,
    keywords: [
      'vibration', 'haptic', 'intensity', 'feedback', 'touch', 'tactile', //
      'drag', 'swipe', 'vibrate', 'motor', 'click',
    ],
  ),
  SettingEntry(
    page: SettingsPage.permissions,
    title: 'Permissions',
    subtitle: 'Notifications, audio, and file access',
    category: 'DEVELOPER TOOLS',
    icon: Icons.shield_outlined,
    keywords: [
      'permissions', 'audio', 'mic', 'microphone', 'record', //
      'storage permission', 'notification', 'android', 'macos', 'access',
      'privacy', 'security', 'record_audio', 'grant', 'allow',
    ],
  ),
  SettingEntry(
    page: SettingsPage.database,
    title: 'Database Management',
    subtitle: 'Wipe database, compute DSP & PCA',
    category: 'DEVELOPER TOOLS',
    icon: Icons.dns_rounded,
    keywords: [
      'database', 'db', 'wipe', 'reset', 'pca', 'dsp computation', //
      'cache', 'clear', 'sqlite', 'index', 'rebuild', 'features',
      'tracks.db', 'downloads.db', 'purge',
    ],
  ),
  SettingEntry(
    page: SettingsPage.advanced,
    title: 'Advanced',
    subtitle: 'Edit TOML config and data maintenance',
    category: 'DEVELOPER TOOLS',
    icon: Icons.terminal_rounded,
    keywords: [
      'toml', 'config', 'advanced', 'raw', 'editor', 'maintenance', //
      'configuration', 'config.toml', 'file', 'text editor', 'export',
      'backup', 'import', 'developer', 'lobotomize', 'fresh install',
      'reset', 'onboarding test', 'demo', 'sample results', 'mock search',
    ],
  ),
  SettingEntry(
    page: SettingsPage.about,
    title: 'About',
    subtitle: 'App version and developer info',
    category: 'ABOUT',
    icon: Icons.info_outline_rounded,
    keywords: [
      'about', 'version', 'developer', 'info', 'github', 'release', //
      'credits', 'license', 'update', 'mai an lab', 'streamrip', 'build',
    ],
  ),
];

/// settings.py `_render_hub_or_search` for a non-empty [query]: the direct
/// matches (substring either way, or every word somewhere), then the
/// typo-tolerant ones, each entry once.
List<SettingEntry> searchSettings(
  String query, [
  List<SettingEntry> entries = kSettingEntries,
]) {
  final q = query.trim().toLowerCase();
  final terms = q.split(_ws).where((t) => t.length > 1).toList();
  final direct = <SettingEntry>[];
  for (final e in entries) {
    final title = e.title.toLowerCase();
    final sub = e.subtitle.toLowerCase();
    final cat = e.category.toLowerCase();
    final kws = [for (final k in e.keywords) k.toLowerCase()];
    if (title.contains(q) ||
        sub.contains(q) ||
        cat.contains(q) ||
        kws.any((k) => k.contains(q)) ||
        kws.any(q.contains)) {
      direct.add(e);
    } else if (terms.isNotEmpty &&
        terms.every(
          (t) =>
              title.contains(t) ||
              sub.contains(t) ||
              cat.contains(t) ||
              kws.any((k) => k.contains(t)),
        )) {
      direct.add(e);
    }
  }
  final out = <SettingEntry>[...direct];
  for (final (_, e) in semanticMatches(query, entries)) {
    if (!out.contains(e)) out.add(e);
  }
  return out;
}

final _ws = RegExp(r'\s+');

const _weights = {
  'disable': 3.0, 'enable': 3.0, 'turn': 2.0, 'off': 2.5, 'on': 2.5, //
  'change': 2.0, 'set': 2.0, 'boost': 2.5, 'wipe': 3.0, 'clear': 2.5,
  'path': 2.5, 'folder': 2.5, 'bass': 3.0, 'treble': 3.0, 'eq': 3.0,
  'equalizer': 3.0, 'jarvis': 3.0, 'qobuz': 3.0, 'key': 2.5, 'token': 2.5,
  'vibration': 2.8, 'haptic': 2.8, 'metadata': 3.0, 'database': 2.8,
};

/// settings.py `_get_semantic_matches`: weighted word-count cosine, plus
/// 0.15 per query word of 4+ letters within one edit of a keyword; scores
/// of 0.15 and up, best first (a stable sort, as Python's).
List<(double, SettingEntry)> semanticMatches(
  String query, [
  List<SettingEntry> entries = kSettingEntries,
]) {
  final q = query.trim().toLowerCase();
  final qTokens = q.split(_ws).where((t) => t.isNotEmpty).toList();
  if (qTokens.isEmpty) return const [];
  final qWords = qTokens.where((w) => w.length > 1).toList();
  Map<String, double> vec(Iterable<String> tokens) {
    final v = <String, double>{};
    for (final t in tokens) {
      v[t] = (v[t] ?? 0) + (_weights[t] ?? 1.0);
    }
    return v;
  }

  double norm(Map<String, double> v) {
    final n = math.sqrt(v.values.fold(0.0, (a, x) => a + x * x));
    return n == 0 ? 1.0 : n;
  }

  final qVec = vec(qTokens);
  final qNorm = norm(qVec);
  final scores = <(double, SettingEntry)>[];
  for (final e in entries) {
    final doc = '${e.title} ${e.subtitle} ${e.keywords.join(' ')}'
        .toLowerCase()
        .split(_ws)
        .where((t) => t.isNotEmpty);
    final dVec = vec(doc);
    final dNorm = norm(dVec);
    var dot = 0.0;
    qVec.forEach((t, w) => dot += w * (dVec[t] ?? 0.0));
    var score = dot / (qNorm * dNorm);
    for (final w in qWords) {
      if (w.length < 4) continue;
      if (e.keywords.any((k) => levenshtein(w, k.toLowerCase()) <= 1)) {
        score += 0.15;
      }
    }
    if (score >= 0.15) scores.add((score, e));
  }
  mergeSort(scores, compare: (a, b) => b.$1.compareTo(a.$1));
  return scores;
}

/// utils/text_match.py `levenshtein_distance`.
int levenshtein(String a, String b) {
  if (a.length < b.length) return levenshtein(b, a);
  if (b.isEmpty) return a.length;
  var prev = List<int>.generate(b.length + 1, (i) => i);
  for (var i = 0; i < a.length; i++) {
    final cur = [i + 1];
    for (var j = 0; j < b.length; j++) {
      cur.add(
        math.min(
          math.min(prev[j + 1] + 1, cur[j] + 1),
          prev[j] + (a[i] == b[j] ? 0 : 1),
        ),
      );
    }
    prev = cur;
  }
  return prev.last;
}

/// A stable sort (List.sort is not guaranteed stable).
void mergeSort<T>(List<T> list, {required int Function(T, T) compare}) {
  if (list.length < 2) return;
  final mid = list.length ~/ 2;
  final left = list.sublist(0, mid);
  final right = list.sublist(mid);
  mergeSort(left, compare: compare);
  mergeSort(right, compare: compare);
  var i = 0, j = 0, k = 0;
  while (i < left.length && j < right.length) {
    list[k++] = compare(right[j], left[i]) < 0 ? right[j++] : left[i++];
  }
  while (i < left.length) {
    list[k++] = left[i++];
  }
  while (j < right.length) {
    list[k++] = right[j++];
  }
}
