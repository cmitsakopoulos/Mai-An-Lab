import 'dart:async';

import 'package:flutter/foundation.dart';

import '../assistant/web_research.dart' show ResearchUnavailable;
import '../core/progress_notify.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import '../data/library_repository.dart';
import '../data/metadata_queries.dart' show genreName, kGapNone;
import '../player/queue_store.dart' show Prefs;
import '../search/new_releases.dart' show kMusicBrainzDefaultContact;
import '../sources/source_client.dart';
import '../stations/genre_taxonomy.dart';
import 'metadata_ai.dart';
import 'metadata_enrich.dart';
import 'metadata_qobuz.dart';
import 'metadata_service.dart';
import 'musicbrainz.dart';

/// A snackbar's tone (`show_snackbar(color=…)`).
enum WbTone { info, success, warn, error, ai }

/// The state and actions of `metadata_workbench.py`'s
/// `MetadataWorkbenchPane`: every artist in one confidence-ordered list,
/// search, the Sync and AI research runs, accepting suggestions, the
/// audit's review answers, bulk genre edits, the per-artist editor's
/// saves, merges, and MusicBrainz matches. One instance lives for the
/// process (Flet reuses its pane), so a running Sync survives leaving the
/// page.
class WorkbenchController extends ChangeNotifier {
  WorkbenchController({
    required this.repository,
    required this.service,
    required this.researcher,
    required this.prefs,
    this.qobuzClient,
    MusicBrainzClient Function()? musicBrainz,
    ProgressNotifier Function(String title, void Function() onCancel)? notifier,
  }) : _musicBrainz =
           musicBrainz ??
           (() => MusicBrainzClient(contact: kMusicBrainzDefaultContact)),
       _notifier =
           notifier ??
           ((title, onCancel) =>
               ProgressNotifier('metadata', title, onCancel: onCancel));

  final LibraryRepository? Function() repository;
  final MetadataService service;
  final MetadataResearcher Function(LibraryRepository repo) researcher;
  final Prefs prefs;
  final Future<SourceClient> Function()? qobuzClient;
  final MusicBrainzClient Function() _musicBrainz;
  final ProgressNotifier Function(String title, void Function() onCancel)
  _notifier;

  /// Snackbars for whichever screen shows the workbench.
  final _snacks = StreamController<(String, WbTone)>.broadcast();
  Stream<(String, WbTone)> get snacks => _snacks.stream;
  void _snack(String text, [WbTone tone = WbTone.info]) {
    if (!_snacks.isClosed) _snacks.add((text, tone));
  }

  /// An artist whose editor should open (`focus_artist`, a reload's
  /// pending focus); the view consumes it.
  final _openEditor = StreamController<Map<String, Object?>>.broadcast();
  Stream<Map<String, Object?>> get openEditor => _openEditor.stream;

  // ── data ──
  Map<String, Object?> coverage = const {};
  List<Map<String, Object?>> everyone = const [];
  List<Map<String, Object?>> allGaps = const [];
  List<Map<String, Object?>> lowConf = const [];
  List<Map<String, Object?>> reviewRows = const [];
  Map<String, List<Map<String, Object?>>> reviewByName = const {};
  List<Map<String, Object?>> vocab = const [];
  List<Map<String, Object?>> countries = const [];
  Map<String, Object?> aiPending = {};
  Set<String> researched = {};
  bool aiReady = false;
  bool loaded = false;

  // ── view state ──
  String sort = 'least';
  String search = '';
  final selected = <String>{};
  String? _pendingFocus;

  // ── runs ──
  bool syncing = false;
  bool researching = false;
  bool _cancel = false;
  int sCur = 0, sTotal = 0, sOk = 0, sBlank = 0, sErr = 0;
  String sName = '';
  int rCur = 0, rTotal = 0, rFound = 0, rUnknown = 0;
  String rName = '';
  bool rSearching = false;

  String get syncProvider {
    final p = prefs.getString('sync_provider', 'musicbrainz');
    return p == 'qobuz' ? 'qobuz' : 'musicbrainz';
  }

  set syncProvider(String p) {
    if (p == syncProvider) return;
    prefs.set('sync_provider', p).ignore();
    notifyListeners();
  }

  LibraryRepository? get _repo {
    final r = repository();
    return r != null && r.writable ? r : null;
  }

  // ── loading ───────────────────────────────────────────────────────────────

  /// `_reload_async`: one full-library gap scan shared with coverage, the
  /// review rows, the vocabulary, countries and AI suggestions.
  ///
  /// Calls while one is running share it: the Library's edit button asks
  /// for a focused reload just as the page opening asks for its own.
  Future<void> reload() =>
      _reloading ??= _reload().whenComplete(() => _reloading = null);
  Future<void>? _reloading;

  /// The editor [focusArtist] found before the page was listening.
  Map<String, Object?>? _waitingEditor;

  /// The page, once subscribed to [openEditor]: an editor still to open.
  Map<String, Object?>? takeWaitingEditor() {
    final row = _waitingEditor;
    _waitingEditor = null;
    return row;
  }

  Future<void> _reload() async {
    final repo = _repo;
    if (repo == null) {
      loaded = true;
      notifyListeners();
      return;
    }
    try {
      everyone = await repo.metadataGapArtists(limit: 100000, everything: true);
      allGaps = [
        for (final g in everyone)
          if (((g['gap_severity'] as int?) ?? 0) < kGapNone &&
              g['source'] != 'manual')
            g,
      ];
      coverage = await repo.metadataCoverage(gaps: allGaps);
      lowConf = await repo.lowConfidenceArtists(500);
      reviewRows = await _buildReviewRows(repo);
      reviewByName = {
        for (final r in reviewRows)
          r['artist_name']! as String: (r['review']! as List)
              .cast<Map<String, Object?>>(),
      };
      vocab = await repo.genreVocabulary(300);
      countries = await repo.libraryCountries();
      aiPending = Map.of(await repo.pendingAiResearch());
      researched = await repo.researchedArtistNames();
      aiReady = await researcher(repo).available();
    } catch (e) {
      debugPrint('Metadata workbench load failed: $e');
    }
    loaded = true;
    notifyListeners();
    final focus = _pendingFocus;
    if (focus != null) {
      _pendingFocus = null;
      Map<String, Object?>? row;
      for (final g in everyone) {
        if (g['artist_name'] == focus) {
          row = g;
          break;
        }
      }
      row ??= await repo.artistWorkbenchItem(focus);
      if (row == null || _openEditor.isClosed) return;
      if (_openEditor.hasListener) {
        _openEditor.add(row);
      } else {
        _waitingEditor = row;
      }
    }
  }

  /// `_build_review_rows`: unsure MusicBrainz matches, the audit's flags and
  /// look-alike names, one row per artist, most tracks first.
  Future<List<Map<String, Object?>>> _buildReviewRows(
    LibraryRepository repo,
  ) async {
    final rows = <String, Map<String, Object?>>{};
    void add(Map<String, Object?> item, Map<String, Object?> reason) {
      final row = rows.putIfAbsent(
        item['artist_name']! as String,
        () => {...item, 'review': <Map<String, Object?>>[]},
      );
      (row['review']! as List).add(reason);
    }

    for (final item in lowConf) {
      add(item, {
        'kind': 'mb',
        'text': "MusicBrainz wasn't sure about this match.",
      });
    }
    Map<String, List<Map<String, Object?>>> flags;
    List<Map<String, Object?>> dups;
    try {
      flags = await repo.auditFlags();
      dups = await repo.duplicateArtists();
    } catch (e) {
      debugPrint('Metadata audit failed: $e');
      flags = {};
      dups = [];
    }
    Future<Map<String, Object?>?> item(String name) async {
      final have = rows[name];
      if (have != null) return have;
      try {
        return await repo.artistWorkbenchItem(name);
      } catch (e) {
        debugPrint("Review: couldn't load $name: $e");
        return null;
      }
    }

    for (final MapEntry(key: name, value: fl) in flags.entries) {
      final it = await item(name);
      if (it == null) continue;
      for (final f in fl) {
        add(it, {'kind': 'audit', 'text': f['reason'], ...f});
      }
    }
    for (final dup in dups) {
      final it = await item(dup['alias']! as String);
      if (it != null) {
        add(it, {
          'kind': 'dup',
          'text': 'Same artist as ${dup['canonical']}?',
          ...dup,
        });
      }
    }
    final out = rows.values.toList();
    final idx = [for (var i = 0; i < out.length; i++) i]
      ..sort((a, b) {
        final c = _tracks(out[b]).compareTo(_tracks(out[a]));
        return c != 0 ? c : a.compareTo(b);
      });
    return [for (final i in idx) out[i]];
  }

  static int _tracks(Map<String, Object?> g) =>
      (g['track_count'] as num?)?.toInt() ?? 0;

  /// `focus_artist`: the Library's edit button lands here, on that artist.
  Future<void> focusArtist(String name) {
    _pendingFocus = name;
    search = name;
    return reload();
  }

  /// `filter_genre`: the Genres view's curate button.
  Future<void> filterGenre(String genre) {
    _pendingFocus = null;
    search = genre;
    return reload();
  }

  void setSearch(String q) {
    if (q == search) return;
    search = q;
    notifyListeners();
  }

  void setSort(String key) {
    if (key == sort) return;
    sort = key;
    notifyListeners();
  }

  // ── derived ───────────────────────────────────────────────────────────────

  /// `_research_targets`: unplaceable artists nobody has researched.
  List<String> researchTargets() => [
    for (final g in allGaps)
      if (g['gap_severity'] == 0 &&
          !researched.contains(pyLower(g['artist_name']! as String)))
        g['artist_name']! as String,
  ];

  /// `_suggestion_rows`: an AI answer waiting, or a tag the files carry.
  List<Map<String, Object?>> suggestionRows() => [
    for (final g in allGaps)
      if (aiPending.containsKey(g['artist_name']) || g['gap_severity'] == 1) g,
  ];

  /// `_suggestion_of`: (genres, country, origin) Accept would write.
  (List<String>, String, String) suggestionOf(Map<String, Object?> g) {
    final ai = aiPending[g['artist_name']];
    if (ai is Map && pyTruthy(ai)) {
      return (
        [for (final x in (ai['genres'] as List?) ?? const []) '$x'],
        pyTruthy(ai['country']) ? '${ai['country']}' : '',
        'ai',
      );
    }
    final files = [
      for (final f in ((g['source_genres'] as List?) ?? const []).take(3))
        pyLower('$f'),
    ];
    return (files, '', 'files');
  }

  /// `_visible`: every artist (search matches name, genres or file tags),
  /// in confidence order.
  List<Map<String, Object?>> visible() {
    final q = pyLower(pyStrip(search));
    var pool = everyone;
    if (q.isNotEmpty) {
      pool = [
        for (final g in pool)
          if (pyLower((g['artist_name'] as String?) ?? '').contains(q) ||
              genreNames(g['genres']).any((n) => pyLower(n).contains(q)) ||
              ((g['source_genres'] as List?) ?? const []).any(
                (n) => pyLower('$n').contains(q),
              ))
            g,
      ];
    }
    final keyed = [for (final (i, g) in pool.indexed) (_sortKey(g), i, g)];
    keyed.sort((a, b) {
      for (var k = 0; k < 3; k++) {
        final c = a.$1[k].compareTo(b.$1[k]);
        if (c != 0) return c;
      }
      return a.$2.compareTo(b.$2);
    });
    return [for (final (_, _, g) in keyed) g];
  }

  /// `_sure_tier`: 0 (least sure) … 7 (set by hand).
  int sureTier(Map<String, Object?> g) {
    final kinds = {
      for (final r in reviewByName[g['artist_name']] ?? const []) r['kind'],
    };
    if (kinds.contains('audit') || kinds.contains('dup')) return 0;
    if (kinds.contains('mb')) return 1;
    if (g['source'] == 'manual') return 7;
    final sev = (g['gap_severity'] as int?) ?? 2;
    if (sev < kGapNone) return 2 + sev;
    final prov = g['provenance'];
    return prov is Map && prov['genres'] == 'ai' ? 5 : 6;
  }

  List<num> _sortKey(Map<String, Object?> g) {
    final tier = sureTier(g);
    final score = (g['score'] as num?) ?? 0;
    final tracks = -_tracks(g);
    return sort == 'least' ? [tier, score, tracks] : [-tier, -score, tracks];
  }

  /// `_fix_of`: the audit's own answer for a flagged row.
  Map<String, Object?>? fixOf(String name) {
    for (final r in reviewByName[name] ?? const <Map<String, Object?>>[]) {
      if (r['kind'] == 'audit' && pyTruthy(r['suggest_country'])) return r;
    }
    return null;
  }

  // ── selection ─────────────────────────────────────────────────────────────

  void toggleSelect(String name, bool on) {
    on ? selected.add(name) : selected.remove(name);
    notifyListeners();
  }

  void clearSelection() {
    selected.clear();
    notifyListeners();
  }

  /// `_review_suggestions`: every row with a suggestion waiting, selected.
  void reviewSuggestions() {
    selected
      ..clear()
      ..addAll([for (final g in suggestionRows()) g['artist_name']! as String]);
    search = '';
    notifyListeners();
  }

  // ── Sync ──────────────────────────────────────────────────────────────────

  /// `_start_sync` / `_do_sync`: the manual Sync against the chosen source,
  /// parked and incomplete artists included.
  Future<void> startSync() async {
    if (syncing || researching) return;
    final deps = service.deps();
    if (deps == null) return;
    syncing = true;
    _cancel = false;
    sCur = sTotal = sOk = sBlank = sErr = 0;
    sName = 'Starting…';
    notifyListeners();
    // One pass at a time: the automatic one finishes first.
    if (service.running) {
      sName = 'Waiting for the automatic pass…';
      notifyListeners();
      await service.whenIdle();
    }
    final notifier = _notifier('Syncing artist metadata', cancel);
    final provider = syncProvider;
    Map<String, Object?> summary = {};
    try {
      summary = await enrichLibrary(
        deps,
        provider: provider,
        includeFailed: true,
        retryIncomplete: true,
        cancelled: () => _cancel,
        progress: (i, total, name, res) {
          notifier.update(i, total, metadataProgressText(i, total, name));
          sCur = i;
          sTotal = total;
          sName = name;
          final reason = res['reason'];
          if (res['status'] == 'error' ||
              (reason is String && reason.startsWith('error:'))) {
            sErr++;
          } else if (!pyTruthy(res['genres'])) {
            sBlank++;
          } else {
            sOk++;
          }
          notifyListeners();
        },
      );
    } catch (e) {
      debugPrint('Workbench sync failed: $e');
      summary = {'status': 'error: $e'};
    } finally {
      notifier.finish(
        metadataSummaryText(summary),
        metadataRetryAction(summary),
      );
      syncing = false;
      await reload();
      _reportSync(summary, provider);
    }
  }

  /// `_report_sync`: what happened, loudly.
  void _reportSync(Map<String, Object?> summary, String provider) {
    final status = summary['status'];
    final name = pyCapitalize((summary['provider'] as String?) ?? provider);
    if (status is String && status.startsWith('error:')) {
      _snack(
        '$name sync failed: ${pyStrip(status.substring(6))}',
        WbTone.error,
      );
      return;
    }
    if (status == 'aborted') {
      _snack(
        'Stopped after $sCur of $sTotal — connection failed. '
        "Run Sync again when you're back online.",
        WbTone.error,
      );
      return;
    }
    if (status == 'cancelled') {
      _snack(
        'Stopped at $sCur of $sTotal. '
        '${summary['enriched'] ?? 0} artists saved.',
        WbTone.warn,
      );
      return;
    }
    if (status == 'uptodate') {
      _snack('All artists are already up to date on $name.', WbTone.success);
      return;
    }
    final parts = ['$sOk matched'];
    if (sBlank > 0) parts.add('$sBlank blank/unmatched');
    if (sErr > 0) parts.add('$sErr failed');
    _snack('$name Sync done · ${parts.join(', ')}', WbTone.success);
  }

  /// `_cancel_sync`: stops a Sync or a research run between artists.
  void cancel() => _cancel = true;

  // ── AI research ───────────────────────────────────────────────────────────

  /// `_start_research` / `_do_research`: a dozen artists per Gemini call;
  /// answers land as suggestions, nothing is saved until accepted.
  Future<void> startResearch(List<String> names) async {
    if (syncing || researching || names.isEmpty) return;
    final repo = _repo;
    if (repo == null) return;
    researching = true;
    _cancel = false;
    rCur = 0;
    rTotal = names.length;
    rFound = rUnknown = 0;
    rName = 'Starting…';
    rSearching = false;
    notifyListeners();
    final notifier = _notifier('Researching artists', cancel);
    Map<String, Object?> summary = {};
    try {
      summary = await researcher(repo).researchArtists(
        names,
        cancelled: () => _cancel,
        progress: (i, total, name, res) {
          rCur = i;
          rTotal = total;
          rName = name;
          final st = res['status'];
          if (st == 'searching') {
            rSearching = true;
            notifyListeners();
            return;
          }
          rSearching = false;
          if (st == 'ok') {
            rFound++;
          } else if (st != 'skipped') {
            rUnknown++;
          }
          notifier.update(i, total, '$i / $total artists · $name');
          notifyListeners();
        },
      );
    } catch (e) {
      debugPrint('AI research run failed: $e');
      summary = {'status': 'error: $e'};
    } finally {
      final status = (summary['status'] as String?) ?? '';
      final found = (summary['found'] as int?) ?? 0;
      notifier.finish(
        status.startsWith('error')
            ? 'Research failed: ${pyStrip(status.substring(6))}'
            : found > 0
            ? '$found suggestion${found != 1 ? 's' : ''} to review'
            : 'No new suggestions found',
        found > 0 ? ('Review', 'workbench:suggestions') : null,
      );
      researching = false;
      await reload();
      _reportResearch(summary);
    }
  }

  void _reportResearch(Map<String, Object?> summary) {
    final status = (summary['status'] as String?) ?? '';
    final found = (summary['found'] as int?) ?? 0;
    final unknown =
        ((summary['unknown'] as int?) ?? 0) +
        ((summary['failed'] as int?) ?? 0);
    if (status.startsWith('error')) {
      _snack(pyStrip(status.substring(6)), WbTone.error);
      return;
    }
    if (found > 0) reviewSuggestions();
    var msg = found > 0
        ? '$found suggestion${found != 1 ? 's' : ''} ready to review'
        : 'The web had nothing reliable on these artists';
    if (unknown > 0 && found > 0) msg += ' · $unknown not found';
    if (status == 'cancelled') msg = 'Stopped. $msg';
    _snack(msg, found > 0 ? WbTone.ai : WbTone.warn);
  }

  /// `_research_selected`.
  void researchSelected() {
    final names = selected.toList()..sort(pyStrCompare);
    selected.clear();
    startResearch(names).ignore();
  }

  /// The editor's one-artist research (`research_artist`); a flagged
  /// artist's research is told what looks wrong. Throws
  /// [ResearchUnavailable] for the caller to show.
  Future<Map<String, Object?>> researchOne(
    String name, {
    bool force = false,
    String dispute = '',
  }) async {
    final repo = _repo;
    if (repo == null) throw const ResearchUnavailable('Library unavailable.');
    final res = await researcher(
      repo,
    ).researchArtist(name, force: force, dispute: dispute);
    researched.add(pyLower(name));
    if (res['status'] == 'ok') {
      aiPending[name] = res;
    } else {
      aiPending.remove(name);
    }
    notifyListeners();
    return res;
  }

  /// `dismiss_suggestion`.
  Future<void> dismissAi(String name) async {
    final repo = _repo;
    if (repo == null) return;
    await researcher(repo).dismissSuggestion(name);
    aiPending.remove(name);
    notifyListeners();
  }

  Future<Map<String, Object?>?> aiResearch(String name) async =>
      _repo?.aiResearch(name);

  // ── writes ────────────────────────────────────────────────────────────────

  /// `_schedule_walk_refresh`: one rebuild per burst; "Auto-Play updated."
  /// once it has landed.
  void _scheduleWalkRefresh() {
    service.refreshWalkModels().then(
      (_) => _snack('Auto-Play updated.', WbTone.success),
      onError: (Object e) => debugPrint('walk refresh failed: $e'),
    );
  }

  /// `_accept`: each row's suggestion MERGED into what the artist has.
  Future<void> accept(List<String> names) async {
    final repo = _repo;
    if (repo == null) return;
    final byName = {for (final g in allGaps) g['artist_name']: g};
    var n = 0;
    for (final name in names) {
      final g = byName[name];
      if (g == null) continue;
      try {
        if (aiPending.containsKey(name)) {
          await researcher(repo).acceptSuggestion(name);
        } else {
          final (genres, _, _) = suggestionOf(g);
          if (genres.isEmpty) continue;
          await repo.bulkTagArtists([name], genre: genres.join(','));
        }
        n++;
      } catch (e) {
        debugPrint('accept failed for $name: $e');
      }
    }
    selected.removeAll(names);
    await reload();
    if (n > 0) {
      _snack('Accepted $n suggestion${n != 1 ? 's' : ''}', WbTone.success);
      _scheduleWalkRefresh();
    }
  }

  /// `_apply_fixes`: the audit's own answer for each flagged row.
  Future<void> applyFixes(List<String> names) async {
    final repo = _repo;
    if (repo == null) return;
    var n = 0;
    for (final name in names) {
      final r = fixOf(name);
      if (r == null) continue;
      try {
        if (r['rule'] == 'collab') {
          await repo.setArtistsGenres(
            [name],
            [for (final x in (r['suggest_genres'] as List?) ?? const []) '$x'],
            country: r['suggest_country'] as String?,
          );
        } else {
          await repo.bulkTagArtists([
            name,
          ], country: r['suggest_country'] as String?);
        }
        n++;
      } catch (e) {
        debugPrint('fix failed for $name: $e');
      }
    }
    selected.clear();
    await reload();
    if (n > 0) {
      _snack('Fixed $n artist${n != 1 ? 's' : ''}', WbTone.success);
      _scheduleWalkRefresh();
    }
  }

  /// `_looks_right`: every doubt on these rows answered "it's right".
  Future<void> looksRight(List<String> names) async {
    final repo = _repo;
    if (repo == null) return;
    final rows = {for (final g in everyone) g['artist_name']: g};
    var n = 0;
    for (final name in names) {
      final cc = ((rows[name]?['country'] as String?) ?? '').toUpperCase();
      try {
        for (final r in reviewByName[name] ?? const <Map<String, Object?>>[]) {
          await _answer(repo, name, r, cc);
        }
        n++;
      } catch (e) {
        debugPrint('looks-right failed for $name: $e');
      }
    }
    selected.clear();
    await reload();
    if (n > 0) {
      _snack('Kept $n artist${n != 1 ? 's' : ''} as they are', WbTone.success);
    }
  }

  Future<void> _answer(
    LibraryRepository repo,
    String name,
    Map<String, Object?> r,
    String cc,
  ) async {
    switch (r['kind']) {
      case 'audit':
        await repo.dismissAuditFlag(name, r['rule']! as String, cc);
      case 'mb':
        await repo.acceptArtistMatches([name]);
      case 'dup':
        await repo.rejectAliasPair(
          r['alias']! as String,
          r['canonical']! as String,
        );
    }
  }

  /// One review answer from the editor: dismiss an audit flag, vouch for a
  /// MusicBrainz match, or "not the same artist".
  Future<void> answerReview(
    String name,
    Map<String, Object?> r,
    String cc,
  ) async {
    final repo = _repo;
    if (repo == null) return;
    await _answer(repo, name, r, cc);
    await reload();
  }

  /// The genre picker's Save: REPLACE or ADD [tags] (and optionally a
  /// country) for [names].
  Future<void> saveGenres(
    List<String> names,
    List<String> tags, {
    required bool replace,
    String? country,
  }) async {
    final repo = _repo;
    if (repo == null) return;
    final int n;
    try {
      n = replace
          ? await repo.setArtistsGenres(names, tags, country: country)
          : await repo.bulkTagArtists(
              names,
              genre: tags.isEmpty ? null : tags.join(','),
              country: country,
            );
    } catch (e) {
      _snack("Couldn't save: ${_msg(e)}", WbTone.error);
      return;
    }
    if (tags.isNotEmpty) rememberGenres(tags);
    selected.removeAll(names);
    await reload();
    final what = tags.isNotEmpty
        ? tags.join(', ')
        : (country != null ? countryLabel(country) : 'no genres');
    _snack(
      '$n artist${n != 1 ? 's' : ''} ${replace ? '→' : '+'} $what',
      WbTone.success,
    );
    _scheduleWalkRefresh();
  }

  /// The editor's Save: the user's own answer for one artist.
  Future<void> saveArtist(
    String name,
    List<String> genres,
    String country,
  ) async {
    final repo = _repo;
    if (repo == null) return;
    try {
      await repo.setManualArtistEnrichment(
        name,
        country: country.isEmpty ? null : country,
        genres: genres,
      );
    } catch (e) {
      _snack("Couldn't save: ${_msg(e)}", WbTone.error);
      return;
    }
    selected.remove(name);
    await reload();
    _snack('Saved $name', WbTone.success);
    _scheduleWalkRefresh();
  }

  /// `_merge`.
  Future<void> merge(String alias, String canonical) async {
    final repo = _repo;
    if (repo == null) return;
    try {
      await repo.mergeArtists(alias, canonical);
    } catch (e) {
      _snack("Couldn't merge: ${_msg(e)}", WbTone.error);
      return;
    }
    selected.remove(alias);
    await reload();
    _snack(
      'Merged into $canonical. Separate them again from its editor.',
      WbTone.success,
    );
    _scheduleWalkRefresh();
  }

  /// `_separate`.
  Future<void> separate(String alias, String canonical) async {
    final repo = _repo;
    if (repo == null) return;
    try {
      await repo.separateArtist(alias);
    } catch (e) {
      _snack("Couldn't separate: ${_msg(e)}", WbTone.error);
      return;
    }
    await reload();
    _snack('$alias is separate from $canonical again.', WbTone.success);
    _scheduleWalkRefresh();
  }

  /// `_review_reject`: "Reject this match".
  Future<void> rejectMatch(String name) async {
    final repo = _repo;
    if (repo == null) return;
    try {
      await repo.confirmArtistMatch(name, status: 'notfound', score: 0);
    } catch (e) {
      _snack("Couldn't reject: ${_msg(e)}", WbTone.error);
      return;
    }
    selected.remove(name);
    await reload();
    _snack('Rejected $name', WbTone.warn);
    _scheduleWalkRefresh();
  }

  Future<List<String>> aliasesOf(String name) async =>
      await _repo?.aliasesOf(name) ?? const [];

  Future<List<String>> albumTitles(String artist) async =>
      await _repo?.albumTitles(artist) ?? const [];

  // ── other sources ─────────────────────────────────────────────────────────

  /// The MusicBrainz identity picker's candidates: junk last, best first,
  /// eight at most.
  Future<List<Map<String, Object?>>> mbCandidates(String query) async {
    final client = _musicBrainz();
    try {
      final cands = await client.searchCandidates(query);
      final idx = [for (var i = 0; i < cands.length; i++) i]
        ..sort((a, b) {
          final ja = cands[a]['is_junk'] == true ? 1 : 0;
          final jb = cands[b]['is_junk'] == true ? 1 : 0;
          if (ja != jb) return ja.compareTo(jb);
          final c = ((cands[b]['score'] as num?) ?? 0).compareTo(
            (cands[a]['score'] as num?) ?? 0,
          );
          return c != 0 ? c : a.compareTo(b);
        });
      return [for (final i in idx.take(8)) cands[i]];
    } finally {
      client.close();
    }
  }

  /// `_use_mb_candidate`: the pick's full genres/country fetched by MBID
  /// and saved as a confirmed match.
  Future<void> useMbCandidate(String artist, Map<String, Object?> cand) async {
    final repo = _repo;
    if (repo == null) return;
    final mbid = cand['mbid'] as String?;
    var country = cand['country'] as String?;
    var area = cand['area'] as String?;
    var genres = (cand['genres'] as List?)?.cast<Object?>();
    if (mbid != null && mbid.isNotEmpty) {
      final client = _musicBrainz();
      Map<String, Object?> details;
      try {
        details = await client.artistDetails(mbid);
      } catch (_) {
        details = {};
      } finally {
        client.close();
      }
      if (pyTruthy(details['genres'])) {
        genres = (details['genres']! as List).cast<Object?>();
      }
      country = (details['country'] as String?) ?? country;
      area = (details['area'] as String?) ?? area;
    }
    try {
      await repo.confirmArtistMatch(
        artist,
        mbid: mbid,
        country: country,
        area: area,
        genres: genres,
        status: 'ok',
        score: pyTruthy(cand['score']) ? (cand['score']! as num).toInt() : 100,
      );
    } catch (e) {
      _snack("Couldn't save: ${_msg(e)}", WbTone.error);
      return;
    }
    final got = genreNames(genres);
    selected.remove(artist);
    await reload();
    _snack(
      got.isNotEmpty
          ? 'Matched $artist · ${got.take(3).join(', ')}'
          : 'Matched $artist, but MusicBrainz lists no genres — add one by '
                'hand.',
      got.isNotEmpty ? WbTone.success : WbTone.warn,
    );
    _scheduleWalkRefresh();
  }

  /// `_fetch_qobuz_action`'s lookup: Qobuz's genres for one artist (the
  /// editor adds them). Throws when Qobuz can't be used.
  Future<Map<String, Object?>> fetchQobuz(String name) {
    final f = qobuzClient;
    if (f == null) throw StateError('Qobuz is not configured');
    return fetchQobuzArtistMetadata(f, name);
  }

  // ── genre vocabulary helpers ──────────────────────────────────────────────

  List<String> recentGenres() {
    final r = prefs['recent_genres'];
    if (r is! List) return const [];
    return [
      for (final t in r)
        if (pyTruthy(t)) '$t',
    ].take(6).toList();
  }

  void rememberGenres(List<String> tags) {
    final seen = <String>{};
    final recent = [
      for (final t in [...tags, ...recentGenres()])
        if (seen.add(t)) t,
    ].take(6).toList();
    prefs.set('recent_genres', recent).ignore();
  }

  /// `_unknown_genres`: tags Auto-Play has nothing to relate to.
  List<String> unknownGenres(Iterable<String> tags) {
    String canon(String t) => alnumOnly(pyLower(t));
    final known = {for (final v in vocab) canon('${v['name']}')};
    return [
      for (final t in tags)
        if (!known.contains(canon(t)) && nonFamilies.contains(genreBucket(t)))
          t,
    ];
  }

  /// `_genre_hint`.
  String genreHint(List<String> tags) {
    final unknown = unknownGenres(tags);
    if (unknown.isEmpty) return '';
    if (unknown.length == tags.length) {
      return "Auto-Play doesn't know “${unknown.first}” yet. Add an existing "
          "genre it's close to as well, so it can find related artists.";
    }
    return '“${unknown.first}” is new; the other genres tell Auto-Play '
        'where it belongs.';
  }

  @override
  void dispose() {
    _snacks.close();
    _openEditor.close();
    super.dispose();
  }
}

String _msg(Object e) {
  final s = '$e';
  return s.startsWith('DbWorkerException: ') ? s.substring(19) : s;
}

/// `_genre_names`.
List<String> genreNames(Object? items) => [
  for (final x in (items as List?) ?? const []) genreName(x) ?? '',
].where((n) => n.isNotEmpty).toList();

/// `_COUNTRY_NAMES`.
const kCountryNames = {
  'GR': 'Greece', 'US': 'United States', 'GB': 'United Kingdom', //
  'FR': 'France', 'DE': 'Germany', 'IT': 'Italy', 'ES': 'Spain',
  'SE': 'Sweden', 'NO': 'Norway', 'FI': 'Finland', 'DK': 'Denmark',
  'NL': 'Netherlands', 'BE': 'Belgium', 'IE': 'Ireland', 'PT': 'Portugal',
  'CA': 'Canada', 'AU': 'Australia', 'NZ': 'New Zealand', 'JP': 'Japan',
  'KR': 'South Korea', 'CN': 'China', 'TW': 'Taiwan', 'IN': 'India',
  'BR': 'Brazil', 'AR': 'Argentina', 'MX': 'Mexico', 'CL': 'Chile',
  'CO': 'Colombia', 'JM': 'Jamaica', 'CU': 'Cuba', 'NG': 'Nigeria',
  'ZA': 'South Africa', 'GH': 'Ghana', 'KE': 'Kenya', 'ET': 'Ethiopia',
  'MA': 'Morocco', 'SN': 'Senegal', 'EG': 'Egypt', 'IL': 'Israel',
  'TR': 'Türkiye', 'RU': 'Russia', 'UA': 'Ukraine', 'PL': 'Poland',
  'CZ': 'Czechia', 'HU': 'Hungary', 'RO': 'Romania', 'RS': 'Serbia',
  'HR': 'Croatia', 'BG': 'Bulgaria', 'CH': 'Switzerland', 'AT': 'Austria',
  'IS': 'Iceland', 'CV': 'Cape Verde', 'CY': 'Cyprus', 'AL': 'Albania',
  'HK': 'Hong Kong', 'SG': 'Singapore', 'TH': 'Thailand',
  'PH': 'Philippines', 'ID': 'Indonesia', 'MY': 'Malaysia', 'VN': 'Vietnam',
  'PR': 'Puerto Rico',
};

/// `_country_label`.
String countryLabel(String? cc) {
  if (cc == null || cc.isEmpty) return '—';
  return kCountryNames[cc.toUpperCase()] ?? cc.toUpperCase();
}
