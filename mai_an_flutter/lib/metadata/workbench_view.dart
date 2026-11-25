import 'dart:async';

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../assistant/web_research.dart' show ResearchUnavailable;
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../ui/tokens.dart';
import 'workbench_controller.dart';

/// `metadata_workbench.py`'s pane: the hero (status, Sync, Research), the
/// search and confidence sort, every artist as one table, and the tray
/// that appears once rows are ticked. Tapping a row opens its editor.

const kAiColor = Color(0xFF5E5CE6);
const kCountryColor = Color(0xFFBF5AF2);
const _checkW = 34.0;
const _tracksW = 26.0;

/// `_SOURCE_STYLE`.
const _sourceStyle = <String, (String, Color?)>{
  'musicbrainz': ('MB', null),
  'files': ('FILES', Tokens.accentGreen),
  'qobuz': ('QOBUZ', Tokens.amber),
  'manual': ('YOURS', Color(0xFFFFD60A)),
  'ai': ('AI', kAiColor),
};

Color _accent(BuildContext c) => Theme.of(c).colorScheme.primary;

String _thousands(Object? n) {
  final s = '${n ?? 0}';
  final b = StringBuffer();
  for (var i = 0; i < s.length; i++) {
    if (i > 0 && (s.length - i) % 3 == 0) b.write(',');
    b.write(s[i]);
  }
  return b.toString();
}

String _clip(String s, int n) {
  final r = s.runes.toList();
  return r.length <= n ? s : String.fromCharCodes(r.sublist(0, n));
}

/// The Settings → Metadata page.
class WorkbenchPage extends StatefulWidget {
  const WorkbenchPage({super.key, required this.controller});

  final WorkbenchController controller;

  @override
  State<WorkbenchPage> createState() => _WorkbenchPageState();
}

class _WorkbenchPageState extends State<WorkbenchPage> {
  WorkbenchController get c => widget.controller;
  late final _search = TextEditingController(text: c.search);
  final _subs = <StreamSubscription<Object?>>[];

  @override
  void initState() {
    super.initState();
    c.addListener(_changed);
    _subs.add(c.snacks.listen(_showSnack));
    _subs.add(
      c.openEditor.listen((row) {
        if (mounted) showArtistEditor(context, c, row);
      }),
    );
    // A running Sync keeps its state; otherwise the page opens fresh.
    if (!c.syncing && !c.researching) c.reload().ignore();
    if (c.takeWaitingEditor() case final row?) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) showArtistEditor(context, c, row);
      });
    }
  }

  @override
  void dispose() {
    c.removeListener(_changed);
    for (final s in _subs) {
      s.cancel();
    }
    _search.dispose();
    super.dispose();
  }

  void _changed() {
    if (_search.text != c.search) _search.text = c.search;
    if (mounted) setState(() {});
  }

  void _showSnack((String, WbTone) s) {
    if (!mounted) return;
    final (text, tone) = s;
    final color = switch (tone) {
      WbTone.success => _accent(context),
      WbTone.warn => Tokens.amber,
      WbTone.error => Tokens.accentRed,
      WbTone.ai => kAiColor,
      WbTone.info => _accent(context),
    };
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        SnackBar(
          content: Row(
            children: [
              Icon(
                tone == WbTone.error
                    ? Icons.error_outline_rounded
                    : tone == WbTone.ai
                    ? Icons.auto_awesome_rounded
                    : Icons.check_circle_rounded,
                color: color,
                size: 20,
              ),
              const SizedBox(width: 10),
              Expanded(child: Text(text)),
            ],
          ),
        ),
      );
  }

  @override
  Widget build(BuildContext context) {
    if (!c.loaded) {
      return const Center(child: CircularProgressIndicator());
    }
    final rows = c.visible();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: _hero(context),
        ),
        const SizedBox(height: 10),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: _filter(context),
        ),
        const SizedBox(height: 10),
        Expanded(child: _list(context, rows)),
        if (c.selected.isNotEmpty && c.everyone.isNotEmpty) _tray(context),
      ],
    );
  }

  // ── hero ──────────────────────────────────────────────────────────────────

  Widget _hero(BuildContext context) {
    final Widget body;
    if (c.syncing) {
      body = _runBanner(
        label: _clip(
          '${pyCapitalize(c.syncProvider)}: ${c.sCur} of ${c.sTotal} · '
          '${c.sName}',
          64,
        ),
        value: c.sTotal > 0 ? c.sCur / c.sTotal : null,
        color: _accent(context),
        counters: [
          ('matched', c.sOk, Tokens.accentGreen),
          ('no tags found', c.sBlank, Tokens.dim),
          ('failed', c.sErr, Tokens.accentRed),
        ],
      );
    } else if (c.researching) {
      body = _runBanner(
        label: _clip(
          c.rSearching
              ? 'Searching the web · ${c.rName}'
              : 'Researching ${c.rCur} of ${c.rTotal} · ${c.rName}',
          64,
        ),
        value: c.rTotal > 0 ? c.rCur / c.rTotal : null,
        color: kAiColor,
        counters: [
          ('suggested', c.rFound, kAiColor),
          ('not found', c.rUnknown, Tokens.dim),
        ],
      );
    } else {
      final cov = c.coverage;
      final artists = (cov['artists'] as int?) ?? 0;
      final placeable = (cov['placeable'] as int?) ?? 0;
      final blocking = (cov['blocking'] as int?) ?? 0;
      final nSugg = c.suggestionRows().length;
      final targets = c.researchTargets();
      String headline, sub;
      Color tone;
      if (artists == 0) {
        headline = 'No artists yet';
        sub = 'Index your library to begin.';
        tone = Tokens.dim;
      } else if (blocking == 0) {
        headline = 'Every artist is placeable.';
        sub = 'All ${_thousands(artists)} have a genre or a country.';
        tone = Tokens.accentGreen;
      } else {
        headline =
            '${_thousands(placeable)} of ${_thousands(artists)} artists '
            'placeable';
        sub = '$blocking still need a genre or a country.';
        tone = Tokens.amber;
      }
      if (nSugg > 0) {
        sub += ' $nSugg suggestion${nSugg != 1 ? 's' : ''} ready to review.';
      }
      final nDoubt = c.reviewRows.length;
      if (nDoubt > 0) {
        sub +=
            ' $nDoubt look${nDoubt == 1 ? 's' : ''} doubtful (listed first).';
      }
      final actions = <Widget>[_syncButton(context, disabled: artists == 0)];
      if (targets.isNotEmpty && c.aiReady) {
        actions.add(
          WbFilledButton(
            'Research ${targets.length}',
            icon: Icons.auto_awesome_rounded,
            color: kAiColor,
            onTap: () => c.startResearch(targets).ignore(),
          ),
        );
      } else if (nSugg > 0) {
        actions.add(
          WbFilledButton(
            'Review',
            icon: Icons.checklist_rounded,
            onTap: c.reviewSuggestions,
          ),
        );
      }
      body = Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Container(
                width: 3,
                height: 34,
                decoration: BoxDecoration(
                  color: tone,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      headline,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontSize: 14.5,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      sub,
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: Tokens.dim, fontSize: 11.5),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Wrap(
            alignment: WrapAlignment.end,
            spacing: 8,
            runSpacing: 8,
            children: actions,
          ),
          if (targets.isNotEmpty && !c.aiReady) ...[
            const SizedBox(height: 10),
            const Text(
              'Tip: add a Gemini API key in Settings → AI Assistant and the '
              'app can research these artists on the web for you.',
              style: TextStyle(color: Tokens.dim, fontSize: 10.5),
            ),
          ],
        ],
      );
    }
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Tokens.surface,
        borderRadius: BorderRadius.circular(Tokens.radiusCard),
        border: Border.all(color: Tokens.borderSubtle),
      ),
      child: body,
    );
  }

  Widget _runBanner({
    required String label,
    required double? value,
    required Color color,
    required List<(String, int, Color)> counters,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          label,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(
            color: Tokens.text,
            fontSize: 12.5,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: 9),
        ClipRRect(
          borderRadius: BorderRadius.circular(3),
          child: LinearProgressIndicator(
            value: value,
            minHeight: 5,
            color: color,
            backgroundColor: Tokens.surface2,
          ),
        ),
        const SizedBox(height: 9),
        Row(
          children: [
            Expanded(
              child: Wrap(
                spacing: 14,
                runSpacing: 4,
                children: [
                  for (final (label, n, col) in counters)
                    Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          '$n',
                          style: TextStyle(
                            color: col,
                            fontSize: 12.5,
                            fontWeight: FontWeight.w800,
                            fontFeatures: const [FontFeature.tabularFigures()],
                          ),
                        ),
                        const SizedBox(width: 4),
                        Text(
                          label,
                          style: const TextStyle(
                            color: Tokens.dim,
                            fontSize: 10.5,
                          ),
                        ),
                      ],
                    ),
                ],
              ),
            ),
            WbGhostButton('Stop', color: Tokens.accentRed, onTap: c.cancel),
          ],
        ),
      ],
    );
  }

  /// `_sync_button`: "Sync · source" plus the menu that picks the source.
  Widget _syncButton(BuildContext context, {required bool disabled}) {
    final accent = _accent(context);
    final providers = {
      'musicbrainz': ('MusicBrainz', Icons.public_rounded, accent),
      'qobuz': ('Qobuz', Icons.storefront_rounded, Tokens.qobuz),
    };
    final (label, icon, color) = providers[c.syncProvider]!;
    return Opacity(
      opacity: disabled ? 0.5 : 1,
      child: Container(
        decoration: BoxDecoration(
          border: Border.all(color: Tokens.border),
          borderRadius: BorderRadius.circular(Tokens.radiusPill),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            // Narrow phones and large text: the label gives way first.
            Flexible(
              child: InkWell(
                borderRadius: const BorderRadius.horizontal(
                  left: Radius.circular(Tokens.radiusPill),
                ),
                onTap: disabled ? null : () => c.startSync().ignore(),
                child: Padding(
                  padding: const EdgeInsets.only(left: 14, top: 10, bottom: 10),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(icon, size: 15, color: color),
                      const SizedBox(width: 7),
                      Flexible(
                        child: Text(
                          'Sync · $label',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Tokens.text,
                            fontSize: 12.5,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            PopupMenuButton<String>(
              tooltip: 'Choose the sync source',
              color: Tokens.surface2,
              onSelected: (p) => c.syncProvider = p,
              itemBuilder: (_) => [
                for (final MapEntry(key: k, value: (pl, pi, pc))
                    in providers.entries)
                  PopupMenuItem(
                    value: k,
                    child: Row(
                      children: [
                        Icon(
                          k == c.syncProvider ? Icons.check_rounded : pi,
                          size: 15,
                          color: k == c.syncProvider ? pc : Tokens.dim,
                        ),
                        const SizedBox(width: 8),
                        Text(
                          'Sync from $pl',
                          style: TextStyle(
                            color: k == c.syncProvider
                                ? Tokens.text
                                : Tokens.dim,
                            fontSize: 12.5,
                            fontWeight: k == c.syncProvider
                                ? FontWeight.w700
                                : FontWeight.w400,
                          ),
                        ),
                      ],
                    ),
                  ),
              ],
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 8),
                child: Icon(
                  Icons.arrow_drop_down_rounded,
                  size: 20,
                  color: color,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── search + sort ─────────────────────────────────────────────────────────

  Widget _filter(BuildContext context) {
    final field = TextField(
      controller: _search,
      onChanged: c.setSearch,
      style: const TextStyle(color: Tokens.text, fontSize: 13),
      decoration: _inputDecoration(context, 'Find an artist…'),
    );
    final seg = SegmentedButton<String>(
      showSelectedIcon: false,
      segments: const [
        ButtonSegment(
          value: 'least',
          icon: Icon(Icons.arrow_upward_rounded, size: 15),
          label: Text('Least sure first', style: TextStyle(fontSize: 11.5)),
        ),
        ButtonSegment(
          value: 'most',
          icon: Icon(Icons.arrow_downward_rounded, size: 15),
          label: Text('Most sure first', style: TextStyle(fontSize: 11.5)),
        ),
      ],
      selected: {c.sort},
      onSelectionChanged: (s) => c.setSort(s.first),
    );
    return LayoutBuilder(
      builder: (context, box) => box.maxWidth >= 560
          ? Row(
              children: [
                Expanded(flex: 7, child: field),
                const SizedBox(width: 8),
                Expanded(flex: 5, child: seg),
              ],
            )
          : Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [field, const SizedBox(height: 8), seg],
            ),
    );
  }

  // ── list ──────────────────────────────────────────────────────────────────

  Widget _list(BuildContext context, List<Map<String, Object?>> rows) {
    if (rows.isEmpty) return _emptyState();
    final q = pyStrip(c.search);
    final n = rows.length;
    final plural = n != 1 ? 's' : '';
    final caption = q.isNotEmpty
        ? '$n artist$plural matching “$q”'
        : '$n artist$plural — tap one to edit, tick several to edit together';
    return ListView.builder(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
      itemCount: n + 2,
      itemBuilder: (context, i) {
        if (i == 0) {
          return Padding(
            padding: const EdgeInsets.only(left: 4, bottom: 6),
            child: Text(
              caption,
              maxLines: 2,
              style: const TextStyle(
                color: Tokens.textTertiary,
                fontSize: 10.5,
              ),
            ),
          );
        }
        final first = i == 1;
        final last = i == n + 1;
        final child = first ? _header() : _row(context, rows[i - 2], last);
        return Container(
          decoration: BoxDecoration(
            color: Tokens.surface,
            border: Border(
              left: const BorderSide(color: Tokens.borderSubtle),
              right: const BorderSide(color: Tokens.borderSubtle),
              top: first
                  ? const BorderSide(color: Tokens.borderSubtle)
                  : BorderSide.none,
              bottom: last
                  ? const BorderSide(color: Tokens.borderSubtle)
                  : BorderSide.none,
            ),
            borderRadius: BorderRadius.vertical(
              top: first
                  ? const Radius.circular(Tokens.radiusCard)
                  : Radius.zero,
              bottom: last
                  ? const Radius.circular(Tokens.radiusCard)
                  : Radius.zero,
            ),
          ),
          clipBehavior: Clip.hardEdge,
          child: child,
        );
      },
    );
  }

  Widget _header() {
    Text h(String t, {TextAlign? align}) => Text(
      t,
      textAlign: align,
      style: const TextStyle(
        color: Tokens.textTertiary,
        fontSize: 9,
        fontWeight: FontWeight.w800,
      ),
    );
    return Padding(
      padding: const EdgeInsets.only(left: 8, right: 10, top: 9, bottom: 5),
      child: Row(
        children: [
          const SizedBox(width: _checkW),
          const SizedBox(width: 6),
          Expanded(flex: 5, child: h('ARTIST')),
          const SizedBox(width: 6),
          Expanded(flex: 4, child: h('GENRE')),
          const SizedBox(width: 6),
          SizedBox(
            width: _tracksW,
            child: h('♪', align: TextAlign.right),
          ),
        ],
      ),
    );
  }

  Widget _row(BuildContext context, Map<String, Object?> g, bool last) {
    final accent = _accent(context);
    final name = g['artist_name']! as String;
    final sev = (g['gap_severity'] as int?) ?? 2;
    final genres = genreNames(g['genres']);
    final files = (g['source_genres'] as List?) ?? const [];
    final country = ((g['country'] as String?) ?? '').toUpperCase();
    final prov = g['provenance'] is Map
        ? g['provenance']! as Map
        : const <Object?, Object?>{};
    final selected = c.selected.contains(name);

    final ai = c.aiPending[name];
    String gtext;
    Color gcolor;
    String? srcKey;
    if (ai is Map && pyTruthy(ai)) {
      final bits = [
        for (final x in ((ai['genres'] as List?) ?? const []).take(3)) '$x',
        if (pyTruthy(ai['country'])) '${ai['country']}',
      ];
      gtext = bits.isEmpty ? '—' : bits.join(', ');
      gcolor = kAiColor;
      srcKey = 'ai';
    } else if (genres.isNotEmpty) {
      gtext = genres.take(3).join(', ');
      gcolor = Tokens.text;
      srcKey = prov['genres'] as String?;
    } else if (files.isNotEmpty) {
      gtext = '${files.first}';
      gcolor = Tokens.accentGreen;
      srcKey = 'files';
    } else {
      gtext = '—';
      gcolor = Tokens.textTertiary;
    }
    var (badge, badgeColor) = _sourceStyle[srcKey ?? ''] ?? ('', Tokens.dim);
    badgeColor ??= accent;
    if (ai is Map && pyTruthy(ai['confidence'])) {
      badge = 'AI · ${'${ai['confidence']}'.toUpperCase()}';
    }
    final reasons = c.reviewByName[name] ?? const [];
    var edge = sev == 0
        ? Tokens.accentRed
        : (sev == 1 ? Tokens.accentGreen : Colors.transparent);
    if (reasons.isNotEmpty) edge = Tokens.amber;

    Widget nameCell = Row(
      children: [
        if (country.isNotEmpty) ...[
          _CountryTag(country),
          const SizedBox(width: 6),
        ],
        Expanded(
          child: Text(
            name,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: Tokens.text,
              fontSize: 13,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
    if (reasons.isNotEmpty) {
      final more = reasons.length - 1;
      nameCell = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          nameCell,
          const SizedBox(height: 2),
          Text(
            '${reasons.first['text']}${more > 0 ? '  +$more' : ''}',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(color: Tokens.amber, fontSize: 10.5),
          ),
        ],
      );
    }
    return Material(
      color: selected ? accent.withValues(alpha: 0.10) : Colors.transparent,
      child: InkWell(
        onTap: () => showArtistEditor(context, c, g),
        child: Container(
          padding: const EdgeInsets.only(left: 8, right: 10, top: 8, bottom: 8),
          decoration: BoxDecoration(
            border: Border(
              left: BorderSide(color: edge, width: 3),
              bottom: last
                  ? BorderSide.none
                  : const BorderSide(color: Tokens.borderSubtle),
            ),
          ),
          child: Row(
            children: [
              SizedBox(
                width: _checkW,
                child: Checkbox(
                  value: selected,
                  activeColor: accent,
                  checkColor: Tokens.bg,
                  side: const BorderSide(color: Tokens.dim, width: 1.5),
                  onChanged: (v) => c.toggleSelect(name, v ?? false),
                ),
              ),
              const SizedBox(width: 6),
              Expanded(flex: 5, child: nameCell),
              const SizedBox(width: 6),
              Expanded(
                flex: 4,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      gtext,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: gcolor, fontSize: 11.5),
                    ),
                    if (badge.isNotEmpty)
                      Text(
                        badge,
                        style: TextStyle(
                          color: badgeColor,
                          fontSize: 8,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                  ],
                ),
              ),
              const SizedBox(width: 6),
              SizedBox(
                width: _tracksW,
                child: Text(
                  '${g['track_count'] ?? 0}',
                  textAlign: TextAlign.right,
                  style: const TextStyle(
                    color: Tokens.dim,
                    fontSize: 11,
                    fontFeatures: [FontFeature.tabularFigures()],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _emptyState() {
    final (icon, title, sub) = ((c.coverage['artists'] as int?) ?? 0) == 0
        ? (
            Icons.library_music_rounded,
            'Your library is empty.',
            'Index your music to start curating metadata.',
          )
        : pyStrip(c.search).isNotEmpty
        ? (
            Icons.search_off_rounded,
            'No artist by that name.',
            'Try a different spelling, or clear the search.',
          )
        : (
            Icons.check_circle_rounded,
            'Nothing to fix here.',
            'This view is clear.',
          );
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 48),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              size: 38,
              color: icon == Icons.check_circle_rounded
                  ? Tokens.accentGreen
                  : Tokens.dim,
            ),
            const SizedBox(height: 8),
            Text(
              title,
              style: const TextStyle(
                color: Tokens.text,
                fontSize: 15,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              sub,
              textAlign: TextAlign.center,
              style: const TextStyle(color: Tokens.dim, fontSize: 11.5),
            ),
          ],
        ),
      ),
    );
  }

  // ── selection tray ────────────────────────────────────────────────────────

  Widget _tray(BuildContext context) {
    final accent = _accent(context);
    final names = c.selected.toList()..sort(pyStrCompare);
    final flagged = [
      for (final x in names)
        if ((c.reviewByName[x] ?? const []).isNotEmpty) x,
    ];
    final fixable = [
      for (final x in flagged)
        if (c.fixOf(x) != null) x,
    ];
    final waiting = {for (final g in c.suggestionRows()) g['artist_name']};
    final acceptable = [
      for (final x in names)
        if (waiting.contains(x)) x,
    ];
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 6, 14, 10),
      decoration: const BoxDecoration(
        color: Tokens.bg,
        border: Border(top: BorderSide(color: Tokens.borderSubtle)),
      ),
      child: SafeArea(
        top: false,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    '${names.length} selected',
                    style: TextStyle(
                      color: accent,
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                TextButton(
                  onPressed: c.clearSelection,
                  child: const Text(
                    'Clear',
                    style: TextStyle(color: Tokens.dim),
                  ),
                ),
                if (c.aiReady)
                  PopupMenuButton<int>(
                    tooltip: 'More',
                    color: Tokens.surface2,
                    icon: const Icon(
                      Icons.more_horiz_rounded,
                      color: Tokens.dim,
                    ),
                    onSelected: (_) => c.researchSelected(),
                    itemBuilder: (_) => const [
                      PopupMenuItem(
                        value: 0,
                        child: Row(
                          children: [
                            Icon(
                              Icons.auto_awesome_rounded,
                              size: 15,
                              color: kAiColor,
                            ),
                            SizedBox(width: 8),
                            Text(
                              'Research on the web',
                              style: TextStyle(
                                color: Tokens.text,
                                fontSize: 12.5,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
              ],
            ),
            Wrap(
              alignment: WrapAlignment.end,
              spacing: 8,
              runSpacing: 8,
              children: [
                if (acceptable.isNotEmpty)
                  WbGhostButton(
                    'Accept suggestion · ${acceptable.length}',
                    icon: Icons.done_all_rounded,
                    color: acceptable.any(c.aiPending.containsKey)
                        ? kAiColor
                        : Tokens.accentGreen,
                    onTap: () => c.accept(acceptable).ignore(),
                  ),
                if (fixable.isNotEmpty)
                  WbGhostButton(
                    'Apply fix · ${fixable.length}',
                    icon: Icons.auto_fix_high_rounded,
                    color: Tokens.amber,
                    onTap: () => c.applyFixes(fixable).ignore(),
                  ),
                if (flagged.isNotEmpty)
                  WbGhostButton(
                    'Looks right · ${flagged.length}',
                    icon: Icons.check_rounded,
                    color: accent,
                    onTap: () => c.looksRight(flagged).ignore(),
                  ),
                WbFilledButton(
                  'Edit genres…',
                  icon: Icons.edit_rounded,
                  onTap: () => showGenrePicker(context, c, names),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

// ── shared bits ─────────────────────────────────────────────────────────────

InputDecoration _inputDecoration(BuildContext context, String hint) =>
    InputDecoration(
      hintText: hint,
      hintStyle: const TextStyle(color: Tokens.dim, fontSize: 13),
      prefixIcon: const Icon(Icons.search_rounded, size: 20, color: Tokens.dim),
      isDense: true,
      filled: true,
      fillColor: Tokens.surface2,
      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: Tokens.border),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: BorderSide(color: _accent(context)),
      ),
    );

class _CountryTag extends StatelessWidget {
  const _CountryTag(this.code);
  final String code;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
    decoration: BoxDecoration(
      color: kCountryColor.withValues(alpha: 0.14),
      borderRadius: BorderRadius.circular(4),
    ),
    child: Text(
      code,
      style: const TextStyle(
        color: kCountryColor,
        fontSize: 9.5,
        fontWeight: FontWeight.w700,
      ),
    ),
  );
}

/// `_filled_btn`.
class WbFilledButton extends StatelessWidget {
  const WbFilledButton(
    this.text, {
    super.key,
    required this.onTap,
    this.icon,
    this.color,
    this.disabled = false,
  });

  final String text;
  final VoidCallback onTap;
  final IconData? icon;
  final Color? color;
  final bool disabled;

  @override
  Widget build(BuildContext context) {
    final bg = color ?? _accent(context);
    return Material(
      color: disabled ? bg.withValues(alpha: 0.4) : bg,
      borderRadius: BorderRadius.circular(Tokens.radiusPill),
      child: InkWell(
        borderRadius: BorderRadius.circular(Tokens.radiusPill),
        onTap: disabled ? null : onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[
                Icon(icon, size: 16, color: Tokens.bg),
                const SizedBox(width: 7),
              ],
              Flexible(
                child: Text(
                  text,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.bg,
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// `_ghost_btn`.
class WbGhostButton extends StatelessWidget {
  const WbGhostButton(
    this.text, {
    super.key,
    required this.onTap,
    this.icon,
    this.color = Tokens.dim,
  });

  final String text;
  final VoidCallback onTap;
  final IconData? icon;
  final Color color;

  @override
  Widget build(BuildContext context) => Material(
    color: Colors.transparent,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(10),
      side: const BorderSide(color: Tokens.border),
    ),
    child: InkWell(
      borderRadius: BorderRadius.circular(10),
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (icon != null) ...[
              Icon(icon, size: 15, color: color),
              const SizedBox(width: 7),
            ],
            Flexible(
              child: Text(
                text,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: color,
                  fontSize: 12.5,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
      ),
    ),
  );
}

/// A chip: filled when [on] (with ✕), outlined otherwise.
class WbChip extends StatelessWidget {
  const WbChip(
    this.label, {
    super.key,
    required this.on,
    required this.color,
    required this.onTap,
    this.prefix = '',
  });

  final String label, prefix;
  final bool on;
  final Color color;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Material(
    color: on ? color : Colors.transparent,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(Tokens.radiusPill),
      side: BorderSide(color: on ? color : color.withValues(alpha: 0.4)),
    ),
    child: InkWell(
      borderRadius: BorderRadius.circular(Tokens.radiusPill),
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 6),
        child: Text(
          '$prefix$label${on ? '  ✕' : ''}',
          style: TextStyle(
            color: on ? Tokens.bg : color,
            fontSize: 12,
            fontWeight: on ? FontWeight.w600 : FontWeight.w400,
          ),
        ),
      ),
    ),
  );
}

Widget _section(String label, IconData icon, Color accent, Widget content) =>
    Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Icon(icon, size: 13, color: accent),
            const SizedBox(width: 6),
            Text(
              label,
              style: TextStyle(
                color: accent,
                fontSize: 10,
                fontWeight: FontWeight.w800,
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        content,
      ],
    );

Widget _sheetHandle() => Center(
  child: Container(
    width: 36,
    height: 5,
    margin: const EdgeInsets.only(top: 10, bottom: 6),
    decoration: BoxDecoration(
      color: Tokens.surfaceElevated,
      borderRadius: BorderRadius.circular(3),
    ),
  ),
);

Future<void> _open(String url) async {
  try {
    await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
  } catch (e) {
    debugPrint('open $url failed: $e');
  }
}

Widget _link(
  String label,
  String url, {
  IconData icon = Icons.open_in_new_rounded,
}) => InkWell(
  borderRadius: BorderRadius.circular(6),
  onTap: () => _open(url),
  child: Padding(
    padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 11, color: kAiColor),
        const SizedBox(width: 3),
        Text(
          _clip(label, 40),
          style: const TextStyle(color: kAiColor, fontSize: 11),
        ),
      ],
    ),
  ),
);

// ── genre picker ────────────────────────────────────────────────────────────

/// `_open_genre_picker`: genres for one or many artists from the library's
/// whole vocabulary (or new ones), REPLACING or ADDED, plus an optional
/// country.
Future<void> showGenrePicker(
  BuildContext context,
  WorkbenchController c,
  List<String> names,
) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: Tokens.surface,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
    ),
    builder: (_) => _GenrePicker(c: c, names: names),
  );
}

class _GenrePicker extends StatefulWidget {
  const _GenrePicker({required this.c, required this.names});
  final WorkbenchController c;
  final List<String> names;

  @override
  State<_GenrePicker> createState() => _GenrePickerState();
}

class _GenrePickerState extends State<_GenrePicker> {
  WorkbenchController get c => widget.c;
  late final List<String> prefill;
  late final List<String> chosen;
  late final Map<String, int> counts;
  String mode = 'replace';
  String country = '';
  final field = TextEditingController();

  @override
  void initState() {
    super.initState();
    final rows = {for (final g in c.everyone) g['artist_name']: g};
    final sets = [
      for (final n in widget.names)
        {for (final x in genreNames(rows[n]?['genres'])) pyLower(x)},
    ];
    final common = sets.isEmpty
        ? <String>{}
        : sets.reduce((a, b) => a.intersection(b));
    prefill = common.toList()..sort(pyStrCompare);
    chosen = List.of(prefill);
    counts = {
      for (final v in c.vocab)
        pyLower('${v['name']}'): (v['artists'] as int?) ?? 0,
    };
  }

  @override
  void dispose() {
    field.dispose();
    super.dispose();
  }

  List<String> get shown => mode == 'replace'
      ? chosen
      : [
          for (final t in chosen)
            if (!prefill.contains(t)) t,
        ];

  void toggle(String t0) {
    final t = pyLower(pyStrip(t0));
    if (t.isEmpty) return;
    setState(() => chosen.contains(t) ? chosen.remove(t) : chosen.add(t));
  }

  void submit(String value) {
    final needle = pyLower(pyStrip(value));
    final hits = needle.isEmpty ? const <String>[] : _hits(needle);
    final parts = hits.isNotEmpty ? [hits.first] : needle.split(',');
    setState(() {
      for (final p0 in parts) {
        final p = pyLower(pyStrip(p0));
        if (p.isNotEmpty && !chosen.contains(p)) chosen.add(p);
      }
      field.clear();
    });
  }

  /// Whole-word and prefix matches first: 'rap' wants 'rap' and 'greek rap'
  /// before 'trap'.
  List<String> _hits(String needle) {
    final hits = [
      for (final v in c.vocab)
        if (pyLower('${v['name']}').contains(needle)) pyLower('${v['name']}'),
    ];
    int b(bool x) => x ? 1 : 0;
    final idx = [for (var i = 0; i < hits.length; i++) i]
      ..sort((x, y) {
        final a = hits[x], z = hits[y];
        final ka = [
          b(a != needle),
          b(!a.startsWith(needle)),
          b(!a.split(' ').any((w) => w.startsWith(needle))),
          -(counts[a] ?? 0),
        ];
        final kz = [
          b(z != needle),
          b(!z.startsWith(needle)),
          b(!z.split(' ').any((w) => w.startsWith(needle))),
          -(counts[z] ?? 0),
        ];
        for (var k = 0; k < 4; k++) {
          final cmp = ka[k].compareTo(kz[k]);
          if (cmp != 0) return cmp;
        }
        return x.compareTo(y);
      });
    return [for (final i in idx) hits[i]];
  }

  Widget _genreRow(String t, {bool create = false}) {
    final accent = _accent(context);
    final on = shown.contains(t);
    final n = counts[t] ?? 0;
    return InkWell(
      onTap: () {
        if (create) field.clear();
        toggle(t);
      },
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 10),
        decoration: const BoxDecoration(
          border: Border(bottom: BorderSide(color: Tokens.borderSubtle)),
        ),
        child: Row(
          children: [
            Icon(
              create
                  ? Icons.add_rounded
                  : on
                  ? Icons.check_circle_rounded
                  : Icons.circle_outlined,
              size: 18,
              color: create
                  ? Tokens.accentGreen
                  : on
                  ? accent
                  : Tokens.dim,
            ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                create ? 'Create “$t”' : t,
                style: TextStyle(
                  color: create ? Tokens.accentGreen : Tokens.text,
                  fontSize: 13.5,
                  fontWeight: on ? FontWeight.w600 : FontWeight.w400,
                ),
              ),
            ),
            if (n > 0 && !create)
              Text(
                '$n',
                style: const TextStyle(
                  color: Tokens.textTertiary,
                  fontSize: 11,
                  fontFeatures: [FontFeature.tabularFigures()],
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _sectionLabel(String label) => Padding(
    padding: const EdgeInsets.only(left: 4, top: 12, bottom: 4),
    child: Text(
      label,
      style: const TextStyle(
        color: Tokens.textTertiary,
        fontSize: 10,
        fontWeight: FontWeight.w800,
      ),
    ),
  );

  @override
  Widget build(BuildContext context) {
    final accent = _accent(context);
    final needle = pyLower(pyStrip(field.text));
    final items = <Widget>[];
    if (needle.isNotEmpty) {
      items.addAll([for (final t in _hits(needle)) _genreRow(t)]);
      if (!counts.containsKey(needle)) {
        items.add(_genreRow(needle, create: true));
      }
    } else {
      final recent = c.recentGenres();
      if (recent.isNotEmpty) {
        items.add(_sectionLabel('RECENT'));
        items.addAll([for (final t in recent) _genreRow(t)]);
      }
      items.add(_sectionLabel('ALL GENRES IN YOUR LIBRARY'));
      items.addAll([
        for (final v in c.vocab) _genreRow(pyLower('${v['name']}')),
      ]);
    }
    final hint = shown.isEmpty ? '' : c.genreHint(shown);
    final k = widget.names.length;
    final who = k == 1 ? widget.names.first : '$k artists';
    final label = mode == 'replace' ? 'Replace genres for $who' : 'Add to $who';
    final ok = mode == 'replace' || shown.isNotEmpty || country.isNotEmpty;
    final title = k == 1 ? widget.names.first : 'Genres for $k artists';
    return SizedBox(
      height: MediaQuery.sizeOf(context).height * 0.88,
      child: Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _sheetHandle(),
            Padding(
              padding: const EdgeInsets.only(left: 16, right: 8),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.close_rounded, size: 20),
                    color: Tokens.dim,
                    tooltip: 'Close',
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: TextField(
                controller: field,
                autofocus: true,
                style: const TextStyle(color: Tokens.text, fontSize: 14),
                decoration: _inputDecoration(
                  context,
                  'Search or create a genre…',
                ),
                onChanged: (_) => setState(() {}),
                onSubmitted: submit,
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 0),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  SegmentedButton<String>(
                    showSelectedIcon: false,
                    segments: const [
                      ButtonSegment(
                        value: 'replace',
                        label: Text(
                          'Replace their genres',
                          style: TextStyle(fontSize: 12),
                        ),
                      ),
                      ButtonSegment(
                        value: 'add',
                        label: Text(
                          'Add to them',
                          style: TextStyle(fontSize: 12),
                        ),
                      ),
                    ],
                    selected: {mode},
                    onSelectionChanged: (s) => setState(() => mode = s.first),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 6,
                    runSpacing: 6,
                    children: shown.isEmpty
                        ? [
                            Text(
                              mode == 'add'
                                  ? 'Nothing picked yet.'
                                  : 'No genres — pick below or type one.',
                              style: const TextStyle(
                                color: Tokens.textTertiary,
                                fontSize: 11.5,
                              ),
                            ),
                          ]
                        : [
                            for (final t in shown)
                              WbChip(
                                t,
                                on: true,
                                color: accent,
                                onTap: () => toggle(t),
                              ),
                          ],
                  ),
                  if (hint.isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Text(
                      hint,
                      style: const TextStyle(color: Tokens.amber, fontSize: 11),
                    ),
                  ],
                ],
              ),
            ),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                children: items,
              ),
            ),
            const Divider(color: Tokens.borderSubtle, height: 1),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text(
                    'COUNTRY (optional — kept if none tapped)',
                    style: TextStyle(
                      color: Tokens.textTertiary,
                      fontSize: 10,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 6,
                    runSpacing: 6,
                    children: [
                      for (final cc in c.countries.take(8))
                        WbChip(
                          '${cc['code']}',
                          on: country == cc['code'],
                          color: kCountryColor,
                          onTap: () => setState(
                            () => country = country == cc['code']
                                ? ''
                                : '${cc['code']}',
                          ),
                        ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Align(
                    alignment: Alignment.centerRight,
                    child: WbFilledButton(
                      label,
                      icon: Icons.check_rounded,
                      disabled: !ok,
                      onTap: () {
                        final tags = List.of(shown);
                        final cc = country.isEmpty ? null : country;
                        final replace = mode == 'replace';
                        Navigator.of(context).pop();
                        c
                            .saveGenres(
                              widget.names,
                              tags,
                              replace: replace,
                              country: cc,
                            )
                            .ignore();
                      },
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── artist editor ───────────────────────────────────────────────────────────

/// `_open_editor`: one artist's genres, country, AI research, review
/// answers, aliases and other sources, in a sheet.
Future<void> showArtistEditor(
  BuildContext context,
  WorkbenchController c,
  Map<String, Object?> item,
) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: Tokens.surface,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
    ),
    builder: (_) => _ArtistEditor(c: c, item: item),
  );
}

class _ArtistEditor extends StatefulWidget {
  const _ArtistEditor({required this.c, required this.item});
  final WorkbenchController c;
  final Map<String, Object?> item;

  @override
  State<_ArtistEditor> createState() => _ArtistEditorState();
}

class _ArtistEditorState extends State<_ArtistEditor> {
  WorkbenchController get c => widget.c;
  late final String name = widget.item['artist_name']! as String;
  late final Set<String> genres = {
    for (final g in genreNames(widget.item['genres'])) pyLower(g),
  };
  late final List<String> files = [
    for (final f in (widget.item['source_genres'] as List?) ?? const []) '$f',
  ];
  late String country = ((widget.item['country'] as String?) ?? '')
      .toUpperCase();
  late final List<Map<String, Object?>> review = [
    ...(widget.item['review'] as List?)?.cast<Map<String, Object?>>() ??
        c.reviewByName[name] ??
        const [],
  ];
  final custom = TextEditingController();

  Map<String, Object?>? aiPayload;
  bool aiBusy = false, aiDismissed = false;
  final dupKeep = <String, String>{};
  final evidence = <String, List<String>>{};
  List<String> aliases = const [];

  @override
  void initState() {
    super.initState();
    final p = c.aiPending[name];
    aiPayload = p is Map<String, Object?> ? p : null;
    for (final r in review) {
      if (r['kind'] == 'dup') {
        dupKeep[r['alias']! as String] = r['canonical']! as String;
      }
    }
    _load();
  }

  Future<void> _load() async {
    if (aiPayload == null) {
      try {
        final cached = await c.aiResearch(name);
        final p = cached?['payload'];
        if (p is Map<String, Object?>) {
          aiPayload = p;
          aiDismissed = cached!['state'] == 'dismissed' && p['status'] == 'ok';
        }
      } catch (_) {}
    }
    for (final r in review) {
      if (r['kind'] == 'dup') {
        for (final a in [r['alias']! as String, r['canonical']! as String]) {
          evidence[a] = await c.albumTitles(a);
        }
      }
    }
    try {
      aliases = await c.aliasesOf(name);
    } catch (_) {}
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    custom.dispose();
    super.dispose();
  }

  void _addCustom(String v0) {
    final v = pyLower(pyStrip(v0));
    setState(() {
      for (final part in v.split(',')) {
        final p = pyStrip(part);
        if (p.isNotEmpty) genres.add(p);
      }
      custom.clear();
    });
  }

  void _use(Iterable<String> gs, [String cc = '', bool toggle = false]) {
    setState(() {
      for (final g in gs) {
        final low = pyLower(g);
        if (toggle && genres.contains(low)) {
          genres.remove(low);
        } else {
          genres.add(low);
        }
      }
      if (cc.isNotEmpty) country = toggle && country == cc ? '' : cc;
    });
  }

  Future<void> _research(bool force) async {
    setState(() => aiBusy = true);
    try {
      final dispute = [
        for (final r in review)
          if (r['kind'] == 'audit') '${r['text']}',
      ].join(' ');
      final res = await c.researchOne(name, force: force, dispute: dispute);
      aiPayload = res;
      aiDismissed = false;
    } on ResearchUnavailable catch (e) {
      _snack(e.message);
    } catch (e) {
      _snack('$e');
    }
    if (mounted) setState(() => aiBusy = false);
  }

  void _snack(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _answer(Map<String, Object?> r) async {
    final cc = ((widget.item['country'] as String?) ?? '').toUpperCase();
    await c.answerReview(name, r, cc);
    if (mounted) setState(() => review.remove(r));
  }

  void _closeThen(Future<void> Function() action) {
    Navigator.of(context).pop();
    action().ignore();
  }

  void _save() {
    final pending = pyLower(pyStrip(custom.text));
    for (final part in pending.split(',')) {
      final p = pyStrip(part);
      if (p.isNotEmpty) genres.add(p);
    }
    final list = genres.toList()..sort(pyStrCompare);
    final cc = country;
    _closeThen(() => c.saveArtist(name, list, cc));
  }

  Future<void> _fetchQobuz() async {
    _snack("Querying Qobuz for '$name'…");
    Map<String, Object?> res;
    try {
      res = await c.fetchQobuz(name);
    } catch (e) {
      _snack('Qobuz error: $e');
      return;
    }
    final q = (res['genres'] as List?) ?? const [];
    if (q.isNotEmpty) {
      var added = 0;
      setState(() {
        for (final g in q) {
          final gn = g is Map ? '${g['name'] ?? ''}' : '$g';
          final tag = pyLower(pyStrip(gn));
          if (tag.isNotEmpty && genres.add(tag)) added++;
        }
      });
      _snack('Qobuz: found ${q.length} genres ($added added)');
    } else {
      final reason = res['reason'];
      _snack(
        "No genres found on Qobuz for '$name'"
        '${pyTruthy(reason) ? ' ($reason)' : ''}',
      );
    }
  }

  // ── sections ──

  Widget _genres(BuildContext context) {
    final accent = _accent(context);
    final needle = pyLower(pyStrip(custom.text));
    final fileSet = {for (final f in files) pyLower(f)};
    final pool = needle.isNotEmpty
        ? [
            for (final v in c.vocab)
              if (pyLower('${v['name']}').contains(needle)) v,
          ]
        : c.vocab.take(12).toList();
    final sorted = genres.toList()..sort(pyStrCompare);
    final hint = c.genreHint(sorted);
    return _section(
      'GENRES',
      Icons.label_rounded,
      accent,
      Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final t in sorted)
                WbChip(
                  t,
                  on: true,
                  color: accent,
                  onTap: () => setState(() => genres.remove(t)),
                ),
              for (final t in files)
                if (!genres.contains(pyLower(t)))
                  WbChip(
                    t,
                    on: false,
                    prefix: '+ ',
                    color: Tokens.accentGreen,
                    onTap: () => setState(() => genres.add(pyLower(t))),
                  ),
              for (final v in pool)
                if (!genres.contains(pyLower('${v['name']}')) &&
                    !fileSet.contains(pyLower('${v['name']}')))
                  WbChip(
                    '${v['name']}',
                    on: false,
                    prefix: '+ ',
                    color: Tokens.dim,
                    onTap: () =>
                        setState(() => genres.add(pyLower('${v['name']}'))),
                  ),
            ],
          ),
          if (hint.isNotEmpty) ...[
            const SizedBox(height: 8),
            Text(
              hint,
              style: const TextStyle(color: Tokens.amber, fontSize: 11),
            ),
          ],
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: SizedBox(
              width: 220,
              child: TextField(
                controller: custom,
                style: const TextStyle(color: Tokens.text, fontSize: 12),
                decoration: _inputDecoration(context, 'Find or add a genre…'),
                onChanged: (_) => setState(() {}),
                onSubmitted: _addCustom,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _country() {
    final codes = [for (final cc in c.countries.take(8)) '${cc['code']}'];
    if (country.isNotEmpty && !codes.contains(country)) {
      codes.insert(0, country);
    }
    return _section(
      'COUNTRY',
      Icons.public_rounded,
      kCountryColor,
      Wrap(
        spacing: 6,
        runSpacing: 6,
        children: [
          for (final code in codes)
            WbChip(
              code,
              on: country == code,
              color: kCountryColor,
              onTap: () =>
                  setState(() => country = country == code ? '' : code),
            ),
        ],
      ),
    );
  }

  Widget _ai() {
    final p = aiPayload;
    final ctrls = <Widget>[];
    var title = 'AI RESEARCH';
    if (aiBusy) {
      ctrls.add(
        const Row(
          children: [
            SizedBox(
              width: 15,
              height: 15,
              child: CircularProgressIndicator(strokeWidth: 2, color: kAiColor),
            ),
            SizedBox(width: 8),
            Text(
              'Searching the web…',
              style: TextStyle(color: Tokens.dim, fontSize: 12),
            ),
          ],
        ),
      );
    } else if (p != null && p['status'] == 'ok' && !aiDismissed) {
      final conf = pyTruthy(p['confidence']) ? '${p['confidence']}' : 'low';
      title = 'AI SUGGESTION · ${'$conf confidence'.toUpperCase()}';
      if (pyTruthy(p['summary'])) {
        ctrls.add(
          Text(
            '${p['summary']}',
            style: const TextStyle(color: Tokens.text, fontSize: 12.5),
          ),
        );
      }
      final matched = '${p['matched_as'] ?? ''}';
      if (matched.isNotEmpty && pyLower(matched) != pyLower(name)) {
        ctrls.add(
          Text(
            'Matched as: $matched',
            style: const TextStyle(color: Tokens.dim, fontSize: 11),
          ),
        );
      }
      final fresh = {
        for (final g in (p['new_genres'] as List?) ?? const []) '$g',
      };
      final gs = [for (final g in (p['genres'] as List?) ?? const []) '$g'];
      final cc = pyTruthy(p['country']) ? '${p['country']}' : '';
      ctrls.add(
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final g in gs)
              WbChip(
                '$g${fresh.contains(g) ? '  (new)' : ''}',
                on: genres.contains(g),
                prefix: genres.contains(g) ? '' : '+ ',
                color: kAiColor,
                onTap: () => _use([g], '', true),
              ),
            if (cc.isNotEmpty)
              WbChip(
                '$cc · ${countryLabel(cc)}',
                on: country == cc,
                prefix: country == cc ? '' : '+ ',
                color: kCountryColor,
                onTap: () => _use(const [], cc, true),
              ),
          ],
        ),
      );
      final origin =
          switch ('${p['origin_mode'] ?? (pyTruthy(p['grounded']) ? 'google_grounding' : '')}') {
            'google_grounding' => 'From Google Search.',
            'web_snippets' =>
              pyTruthy(p['grounded'])
                  ? 'From web search results that mention your albums or tracks.'
                  : 'From web search results — none mention your albums or '
                        "tracks, so check it's the right artist.",
            _ =>
              "From the model's memory — no web search results"
                  '${pyTruthy(p['search_failed']) ? ' (search was unreachable).' : '.'}',
          };
      final basis = switch ('${p['country_basis'] ?? ''}') {
        'web' => 'The search results name this country.',
        'script' => 'Country matches the script of your titles.',
        'memory' => 'Country is from memory only.',
        _ => '',
      };
      final withheld = switch ('${p['country_withheld'] ?? ''}') {
        'contradicted' =>
          'Country withheld: the search results name a different one.',
        'unsupported' => "Country withheld: nothing backs the model's answer.",
        _ => '',
      };
      ctrls.add(
        Text(
          [origin, basis].where((s) => s.isNotEmpty).join(' '),
          style: TextStyle(color: _accent(context), fontSize: 10.5),
        ),
      );
      if (withheld.isNotEmpty) {
        ctrls.add(
          Text(
            withheld,
            style: const TextStyle(color: Tokens.amber, fontSize: 10.5),
          ),
        );
      }
      final sources = [
        for (final s in ((p['sources'] as List?) ?? const []).take(5))
          if (s is Map && pyTruthy(s['uri']))
            _link(
              pyTruthy(s['title']) ? '${s['title']}' : 'source',
              '${s['uri']}',
            ),
      ];
      if (sources.isNotEmpty) {
        ctrls.add(
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: 4,
            runSpacing: 2,
            children: [
              const Text(
                'Sources',
                style: TextStyle(color: Tokens.textTertiary, fontSize: 10),
              ),
              ...sources,
            ],
          ),
        );
      }
      final searches = [
        for (final q in ((p['queries'] as List?) ?? const []).take(3))
          _link(
            '$q',
            'https://www.google.com/search?q='
                '${pyQuote('$q', safe: ' ').replaceAll(' ', '+')}',
            icon: Icons.search_rounded,
          ),
      ];
      if (searches.isNotEmpty) {
        ctrls.add(
          Wrap(
            crossAxisAlignment: WrapCrossAlignment.center,
            spacing: 4,
            runSpacing: 2,
            children: [
              const Text(
                'Google',
                style: TextStyle(color: Tokens.textTertiary, fontSize: 10),
              ),
              ...searches,
            ],
          ),
        );
      }
      ctrls.add(
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            WbGhostButton(
              'Use all',
              icon: Icons.done_all_rounded,
              color: kAiColor,
              onTap: () => _use(gs, cc),
            ),
            WbGhostButton(
              'Dismiss',
              onTap: () async {
                await c.dismissAi(name);
                if (mounted) setState(() => aiDismissed = true);
              },
            ),
            WbGhostButton(
              'Again',
              icon: Icons.refresh_rounded,
              onTap: () => _research(true),
            ),
          ],
        ),
      );
    } else if (p != null) {
      ctrls.add(
        Text(
          p['status'] != 'ok'
              ? 'The web had nothing reliable on $name.'
              : 'Suggestion dismissed.',
          style: const TextStyle(color: Tokens.dim, fontSize: 12),
        ),
      );
      ctrls.add(
        Align(
          alignment: Alignment.centerLeft,
          child: WbGhostButton(
            'Search again',
            icon: Icons.refresh_rounded,
            color: kAiColor,
            onTap: () => _research(true),
          ),
        ),
      );
    } else {
      ctrls.add(
        const Text(
          "Search the web for this artist's genre and country. Nothing is "
          'saved until you accept it.',
          style: TextStyle(color: Tokens.dim, fontSize: 11.5),
        ),
      );
      ctrls.add(
        Align(
          alignment: Alignment.centerLeft,
          child: WbFilledButton(
            'Research on the web',
            icon: Icons.auto_awesome_rounded,
            color: kAiColor,
            onTap: () => _research(false),
          ),
        ),
      );
    }
    return _section(
      title,
      Icons.auto_awesome_rounded,
      kAiColor,
      Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final (i, w) in ctrls.indexed) ...[
            if (i > 0) const SizedBox(height: 8),
            w,
          ],
        ],
      ),
    );
  }

  Widget _evidenceLine(String artist, Object? n) {
    final albums = evidence[artist];
    final count = (n as num?)?.toInt() ?? 0;
    return Text(
      '$artist: ${albums == null || albums.isEmpty ? '…' : albums.join(', ')}'
      ' · $count track${count != 1 ? 's' : ''}',
      maxLines: 2,
      overflow: TextOverflow.ellipsis,
      style: const TextStyle(color: Tokens.dim, fontSize: 11),
    );
  }

  Widget _review(BuildContext context) {
    final accent = _accent(context);
    final parts = <Widget>[];
    for (final r in List.of(review)) {
      parts.add(
        Text(
          '${r['text']}',
          style: const TextStyle(color: Tokens.text, fontSize: 12.5),
        ),
      );
      switch (r['kind']) {
        case 'mb':
          parts.add(
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                WbGhostButton(
                  'Reject this match',
                  icon: Icons.close_rounded,
                  color: Tokens.accentRed,
                  onTap: () => _closeThen(() => c.rejectMatch(name)),
                ),
                WbGhostButton("It's right", onTap: () => _answer(r)),
              ],
            ),
          );
        case 'audit' when r['rule'] == 'collab':
          final sg = [
            for (final x in (r['suggest_genres'] as List?) ?? const []) '$x',
          ];
          final cc = '${r['suggest_country'] ?? ''}';
          parts.add(
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                WbGhostButton(
                  sg.isNotEmpty
                      ? 'Use $cc + ${sg.take(2).join(', ')}'
                      : 'Use $cc, clear its genres',
                  icon: Icons.public_rounded,
                  color: kCountryColor,
                  onTap: () => setState(() {
                    genres
                      ..clear()
                      ..addAll(sg);
                    country = cc;
                  }),
                ),
                WbGhostButton("It's right", onTap: () => _answer(r)),
              ],
            ),
          );
        case 'audit':
          final cc = '${r['suggest_country'] ?? ''}';
          parts.add(
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                if (cc.isNotEmpty)
                  WbGhostButton(
                    'Use $cc',
                    icon: Icons.public_rounded,
                    color: kCountryColor,
                    onTap: () => _use(const [], cc),
                  ),
                WbGhostButton('Country is right', onTap: () => _answer(r)),
              ],
            ),
          );
        case 'dup':
          final alias = r['alias']! as String;
          final canonical = r['canonical']! as String;
          final keep = dupKeep[alias] ?? canonical;
          final other = keep == canonical ? alias : canonical;
          parts.addAll([
            _evidenceLine(alias, r['alias_tracks']),
            _evidenceLine(canonical, r['canonical_tracks']),
            const Text(
              'Keep the name',
              style: TextStyle(
                color: Tokens.textTertiary,
                fontSize: 10,
                fontWeight: FontWeight.w800,
              ),
            ),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final opt in [canonical, alias])
                  WbChip(
                    opt,
                    on: keep == opt,
                    color: accent,
                    onTap: () => setState(() => dupKeep[alias] = opt),
                  ),
              ],
            ),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                WbFilledButton(
                  'Merge',
                  icon: Icons.call_merge_rounded,
                  onTap: () => _closeThen(() => c.merge(other, keep)),
                ),
                WbGhostButton('Not the same', onTap: () => _answer(r)),
              ],
            ),
          ]);
      }
    }
    return _section(
      'REVIEW',
      Icons.flag_rounded,
      Tokens.amber,
      Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final (i, w) in parts.indexed) ...[
            if (i > 0) const SizedBox(height: 8),
            w,
          ],
        ],
      ),
    );
  }

  Widget _aliases(BuildContext context) => _section(
    'ALSO KNOWN AS',
    Icons.call_merge_rounded,
    _accent(context),
    Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final a in aliases)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    a,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Tokens.text, fontSize: 12.5),
                  ),
                ),
                WbGhostButton(
                  'Separate',
                  icon: Icons.call_split_rounded,
                  onTap: () => _closeThen(() => c.separate(a, name)),
                ),
              ],
            ),
          ),
        const Text(
          'Merged in this library only; your files keep their tags.',
          style: TextStyle(color: Tokens.textTertiary, fontSize: 10.5),
        ),
      ],
    ),
  );

  @override
  Widget build(BuildContext context) {
    final accent = _accent(context);
    final showAi = c.aiReady || aiPayload != null;
    final body = <Widget>[
      if (review.isNotEmpty) _review(context),
      if (showAi) _ai(),
      _genres(context),
      _country(),
      if (aliases.isNotEmpty) _aliases(context),
      const Text(
        'OTHER SOURCES',
        style: TextStyle(
          color: Tokens.textTertiary,
          fontSize: 10,
          fontWeight: FontWeight.w800,
        ),
      ),
      Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          WbGhostButton(
            'Find on MusicBrainz',
            icon: Icons.travel_explore_rounded,
            color: accent,
            onTap: () {
              final nav = Navigator.of(context);
              final host = nav.context;
              nav.pop();
              showMbPicker(host, c, name);
            },
          ),
          WbGhostButton(
            'Fetch from Qobuz',
            icon: Icons.cloud_download_rounded,
            color: Tokens.qobuz,
            onTap: _fetchQobuz,
          ),
        ],
      ),
    ];
    return SizedBox(
      height: MediaQuery.sizeOf(context).height * 0.9,
      child: Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _sheetHandle(),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
              child: Row(
                children: [
                  IconButton(
                    icon: const Icon(Icons.close_rounded, size: 20),
                    color: Tokens.dim,
                    tooltip: 'Close',
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Tokens.text,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  WbFilledButton('Save', onTap: _save),
                ],
              ),
            ),
            const Divider(color: Tokens.borderSubtle, height: 1),
            Expanded(
              child: ListView(
                padding: const EdgeInsets.all(16),
                children: [
                  for (final (i, w) in body.indexed) ...[
                    if (i > 0) const SizedBox(height: 14),
                    w,
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── MusicBrainz identity picker ─────────────────────────────────────────────

/// `_open_mb_dialog`: pick the right act; its country and genres are
/// fetched and saved as a confirmed match.
Future<void> showMbPicker(
  BuildContext context,
  WorkbenchController c,
  String artist,
) => showDialog<void>(
  context: context,
  builder: (_) => _MbPicker(c: c, artist: artist),
);

class _MbPicker extends StatefulWidget {
  const _MbPicker({required this.c, required this.artist});
  final WorkbenchController c;
  final String artist;

  @override
  State<_MbPicker> createState() => _MbPickerState();
}

class _MbPickerState extends State<_MbPicker> {
  late final query = TextEditingController(text: widget.artist);
  bool busy = false;
  String? error;
  List<Map<String, Object?>> cands = const [];

  @override
  void initState() {
    super.initState();
    _search();
  }

  @override
  void dispose() {
    query.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    final q = pyStrip(query.text).isEmpty ? widget.artist : pyStrip(query.text);
    setState(() {
      busy = true;
      error = null;
    });
    try {
      cands = await widget.c.mbCandidates(q);
    } catch (e) {
      error = 'Lookup failed — are you offline?';
    }
    if (mounted) setState(() => busy = false);
  }

  Widget _card(Map<String, Object?> cand) {
    final cname = pyTruthy(cand['name']) ? '${cand['name']}' : widget.artist;
    final disamb = pyStrip('${cand['disambiguation'] ?? ''}');
    final cc = cand['country'] as String?;
    final gstr = genreNames(cand['genres']).join(', ');
    final score = (cand['score'] as num?)?.toInt() ?? 0;
    final sc = score >= 80
        ? Tokens.accentGreen
        : score >= 50
        ? Tokens.amber
        : Tokens.accentRed;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Tokens.surface2,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: Tokens.borderSubtle),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              if (cc != null && cc.isNotEmpty) ...[
                _CountryTag(cc.toUpperCase()),
                const SizedBox(width: 6),
              ],
              Expanded(
                child: Text(
                  cname,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    color: Tokens.text,
                    fontSize: 13.5,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(width: 6),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
                decoration: BoxDecoration(
                  color: sc.withValues(alpha: 0.14),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  '$score%',
                  style: TextStyle(
                    color: sc,
                    fontSize: 10,
                    fontWeight: FontWeight.w800,
                  ),
                ),
              ),
            ],
          ),
          if (disamb.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              disamb,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: Tokens.text, fontSize: 11),
            ),
          ],
          const SizedBox(height: 6),
          Text(
            gstr.isEmpty ? 'No genres listed — fetched when you select' : gstr,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: gstr.isEmpty ? Tokens.amber : Tokens.dim,
              fontSize: 10.5,
            ),
          ),
          const SizedBox(height: 6),
          Align(
            alignment: Alignment.centerRight,
            child: WbFilledButton(
              'Use this match',
              icon: Icons.check_rounded,
              onTap: () {
                Navigator.of(context).pop();
                widget.c.useMbCandidate(widget.artist, cand).ignore();
              },
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.sizeOf(context);
    final width = (size.width - 40).clamp(280.0, 420.0);
    final resH = (size.height - 280).clamp(200.0, 380.0);
    return AlertDialog(
      backgroundColor: Tokens.surface,
      title: Text(
        _clip('MusicBrainz · ${widget.artist}', 40),
        style: const TextStyle(
          color: Tokens.text,
          fontSize: 15,
          fontWeight: FontWeight.w700,
        ),
      ),
      content: SizedBox(
        width: width,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'Pick the right act — its country and genres are fetched and '
              'saved as a confirmed match.',
              style: TextStyle(color: Tokens.dim, fontSize: 11),
            ),
            const SizedBox(height: 10),
            TextField(
              controller: query,
              style: const TextStyle(color: Tokens.text, fontSize: 13),
              decoration: const InputDecoration(
                labelText: 'Artist name',
                isDense: true,
              ),
              onSubmitted: (_) => _search(),
            ),
            const SizedBox(height: 10),
            SizedBox(
              height: resH,
              child: busy
                  ? const Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        SizedBox(
                          width: 15,
                          height: 15,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                        SizedBox(width: 8),
                        Text(
                          'Searching MusicBrainz…',
                          style: TextStyle(color: Tokens.dim, fontSize: 12),
                        ),
                      ],
                    )
                  : error != null
                  ? Text(
                      error!,
                      style: const TextStyle(color: Tokens.amber, fontSize: 12),
                    )
                  : cands.isEmpty
                  ? const Text(
                      'No candidates. Try a different spelling.',
                      style: TextStyle(color: Tokens.dim, fontSize: 12),
                    )
                  : ListView.separated(
                      itemCount: cands.length,
                      separatorBuilder: (_, _) => const SizedBox(height: 8),
                      itemBuilder: (_, i) => _card(cands[i]),
                    ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Close'),
        ),
        WbFilledButton('Search', onTap: _search),
      ],
    );
  }
}
