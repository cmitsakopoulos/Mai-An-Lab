import 'dart:async';
import 'dart:math' as math;

import '../core/py_difflib.dart';
import '../core/py_re.dart';
import '../core/py_text.dart';
import '../core/py_values.dart';
import 'intent.dart';
import 'runner_phrases.dart';

/// `assistant_runner.py`'s deterministic half (phase 5, stage c): dispatch,
/// the pending confirmation and choice, anaphora, and every `_handle_*`.
/// The LLM branch (`_dispatch_llm`) is stage d: until then [llm] is unset
/// and requests run their handler directly, as Flet does with the agent
/// off.
///
/// Tracks travel as Flet's dicts (`Map<String, Object?>`): library rows
/// keyed `title`/`artist`/`album`, engine rows keyed `track_title`/
/// `artist_name`/`album_title` (`_to_engine_track`), so every `or` chain
/// and f-string reads them as Python does.

typedef Row = Map<String, Object?>;

/// What the runner drives: the parts of Flet's `audio_engine` it touches.
/// Queue rows are engine dicts.
abstract class AssistantPlayer {
  List<Row> get queue;
  int get currentIndex;
  String get currentPath;
  String get currentTrack;
  String get currentArtist;
  String get currentAlbum;
  double get duration;
  bool get isPlaying;
  bool get isShuffle;
  set isShuffle(bool value);

  void setQueue(List<Row> tracks, {int startIndex = 0});
  void queueLast(Row track);
  void queueNext(Row track);
  void queueLastMany(List<Row> tracks);
  void removeFromQueue(int index);
  void moveQueueItem(int oldIndex, int newIndex);
  void next();
  void previous();
  void pause();
  void play();
  void stop();
  void clearQueue();
}

/// What the runner reads and writes: the `db_manager` methods and
/// `track_graph` calls it makes. Rows are db_manager's dicts.
abstract class AssistantLibrary {
  Future<List<Row>> searchTracksSimple(String query, {required int limit});
  Future<List<Row>> getAllTracks();
  Future<Row?> getTrackFull(String path);

  /// `has_current_features(path, track_graph.FEATURES_VERSION)`.
  Future<bool> hasCurrentFeatures(String path);
  Future<Set<String>> recentPlayedPaths({required int windowSeconds});
  Future<void> recordPlayback(String path, String event, {String? seedPath});
  Future<List<Row>> getMostPlayed({required int limit});

  /// The names `get_all_artists(search_query=query)` lists, in its order.
  Future<List<String>> searchArtistNames(String query);

  /// The path of the first track whose artist is [name] (`COLLATE
  /// NOCASE`), as `_apply_anaphora_resolution`'s query finds it.
  Future<String?> firstTrackPathByArtist(String name);
  Future<int> createPlaylist(String name);
  Future<void> addTrackToPlaylist(int playlistId, String path);

  /// `track_graph.walk(db, seed, length=length, avoid=avoid)`.
  Future<List<String>> walk(
    String seedPath, {
    required int length,
    required Set<String> avoid,
  });

  /// The paths of `track_graph.neighbors(db, seed, k=k,
  /// edge_kind=KIND_ARTIST)`.
  Future<List<String>> artistNeighbors(String seedPath, {required int k});
}

/// `random` as the runner uses it, over one primitive (`_randbelow`) so a
/// test can script it and Python's runner can be given the same script.
abstract class AssistantRandom {
  int randbelow(int n);
  double random();
}

class SystemAssistantRandom implements AssistantRandom {
  SystemAssistantRandom([math.Random? r]) : _r = r ?? math.Random();
  final math.Random _r;

  @override
  int randbelow(int n) => n <= 0 ? 0 : _r.nextInt(n);

  @override
  double random() => _r.nextDouble();
}

extension PyRandomOps on AssistantRandom {
  /// `random.choice`.
  T choice<T>(List<T> xs) => xs[randbelow(xs.length)];

  /// `random.randint(a, b)`.
  int randint(int a, int b) => a + randbelow(b - a + 1);

  /// `random.shuffle`: Fisher–Yates from the end, as CPython draws.
  void shuffle<T>(List<T> xs) {
    for (var i = xs.length - 1; i > 0; i--) {
      final j = randbelow(i + 1);
      final t = xs[i];
      xs[i] = xs[j];
      xs[j] = t;
    }
  }
}

/// `AssistantResponse`.
class AssistantResponse {
  AssistantResponse({
    required this.spoken,
    required this.displayed,
    this.success = true,
    Map<String, Object?>? extras,
    this.action,
    this.deferredPlay = false,
  }) : extras = extras ?? {};

  String spoken;
  String displayed;
  bool success;
  Map<String, Object?> extras;
  String? action;
  bool deferredPlay;
  Intent? intent;
}

typedef ResponseCallback = Future<AssistantResponse> Function();

/// `PendingConfirmation`.
class PendingConfirmation {
  PendingConfirmation({
    required this.prompt,
    this.onYesAction,
    this.onYesMsg = 'On it.',
    this.onNoMsg = 'Understood. Standing by.',
    this.onYes,
    this.onNo,
  });

  final String prompt;
  final String? onYesAction;
  final String onYesMsg;
  final String onNoMsg;
  final ResponseCallback? onYes;
  final ResponseCallback? onNo;
}

/// `ChoiceOption`.
class ChoiceOption {
  ChoiceOption({
    required this.id,
    required this.title,
    this.subtitle = '',
    Row? payload,
  }) : payload = payload ?? {};

  final String id;
  final String title;
  final String subtitle;
  final Row payload;
}

/// `PendingChoice`.
class PendingChoice {
  PendingChoice({required this.prompt, required this.options, this.onSelect});

  final String prompt;
  final List<ChoiceOption> options;
  final Future<AssistantResponse> Function(ChoiceOption)? onSelect;
}

typedef HistoryProvider = List<Object?> Function();

/// `_to_engine_track`.
Row toEngineTrack(Row row) => {
  'path': row['path'],
  'track_title': pyOr([row['title'], row['track_title'], '']),
  'artist_name': pyOr([row['artist'], row['artist_name'], 'Unknown Artist']),
  'album_title': pyOr([row['album'], row['album_title'], 'Unknown Album']),
  'duration': pyOr([row['duration'], 0.0]),
  'image_url': pyOr([row['image_url'], '']),
};

/// `(x or "")` for a value used as text.
String _text(Object? v) => v is String ? v : (pyTruthy(v) ? pyStr(v) : '');

/// `dict.get(key, default)`: the default only when the key is missing.
Object? _getOr(Row m, String key, Object? fallback) =>
    m.containsKey(key) ? m[key] : fallback;

/// The handlers' `({m}m {s}s)` / `({s}s)` suffix.
String _durationSuffix(Object? duration) {
  final d = pyOr([duration, 0.0]);
  if (d is! num || d <= 0) return '';
  final total = d.toInt();
  final m = total ~/ 60, s = total % 60;
  return m > 0 ? ' (${m}m ${s}s)' : ' (${s}s)';
}

/// A stable sort (Python's `sorted`); Dart's `List.sort` isn't stable.
List<T> _stableSorted<T>(List<T> xs, int Function(T, T) compare) {
  final idx = List<int>.generate(xs.length, (i) => i)
    ..sort((i, j) {
      final c = compare(xs[i], xs[j]);
      return c != 0 ? c : i - j;
    });
  return [for (final i in idx) xs[i]];
}

const _fastIntents = {
  intentSkip,
  intentPrev,
  intentPause,
  intentResume,
  intentStop,
  intentClearQueue,
  intentShuffle,
  intentMute,
  intentUnmute,
};

const _digitMap = {
  '1': 0, 'one': 0, 'first': 0, //
  '2': 1, 'two': 1, 'second': 1,
  '3': 2, 'three': 2, 'third': 2,
  '4': 3, 'four': 3, 'fourth': 3,
  '5': 4, 'five': 4, 'fifth': 4,
};

const _yesWords = {
  'yes', 'yeah', 'yep', 'yup', 'yea', 'yesa', 'yess', 'yessir', //
  'sure', 'ok', 'okay', 'alright', 'definitely', 'absolutely',
};
const _noWords = {'no', 'nope', 'nah', 'cancel', 'dont', 'nevermind', 'stop'};

final _wordToken = PyRe(r'\b\w+\b');
final _spaces = PyRe(r'\s+');
final _bold = PyRe(r'\*\*([^*]+?)\*\*');

/// `_ANAPHORA_RE`: longest first, so multi-word phrases beat single words.
final _anaphoraRe = PyRe(
  r'\b('
  r'the\s+same\s+artist'
  r'|those\s+tracks'
  r'|this\s+album'
  r'|(?:this|that)\s+(?:song|track|one)'
  r'|the\s+(?:song|track|tracks|artist|band|album|music)'
  r'|it|this|that|those|these|them|they|their'
  r')\b',
  ignoreCase: true,
);

/// `_PRONOUN_TYPE`: the trigger is type information.
const pronounType = <String, String>{
  'it': 'track',
  'this': 'track',
  'that': 'track',
  'the song': 'track',
  'the track': 'track',
  'the music': 'track',
  'this song': 'track',
  'that song': 'track',
  'this one': 'track',
  'that one': 'track',
  'them': 'artist',
  'they': 'artist',
  'their': 'artist',
  'the band': 'artist',
  'the artist': 'artist',
  'the same artist': 'artist',
  'the album': 'album',
  'this album': 'album',
  'those': 'multi',
  'these': 'multi',
  'the tracks': 'multi',
  'those tracks': 'multi',
};

const _intentDefaultType = {'play_similar': 'track', 'play_more_by': 'artist'};

/// `_INTENT_WEIGHT`: active playback outweighs passive search by about
/// two ranks of recency.
const _intentWeight = <String, double>{
  'play_now': 1.0,
  'queue_add': 1.0,
  'queue_next': 1.0,
  'play_similar': 1.0,
  'play_more_by': 1.0,
  'play_the_usual': 1.0,
  'search_artist': 0.5,
  'search_track': 0.5,
  'search_album': 0.5,
};
const _intentDefaultWeight = 0.3;
const _anaphoraIntentK = 5.0;
const _implicitAnaphoraIntents = {'play_similar', 'play_more_by'};
const _anaphoraConfirmThreshold = 12;
const _anaphoraAmbiguityGap = 2;
const _anaphoraScanWindow = 25;

typedef _Candidate = (int rank, String type, Object? data, Object? srcIntent);

const _weekdays = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
];
const _months = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

String _two(int n) => n.toString().padLeft(2, '0');

/// `_handle_mood_steer`'s profiles: (energy, brightness, bpm, keywords).
const _moodProfiles = <String, (double, double, double, List<String>)>{
  'chill': (
    0.25,
    0.35,
    85.0,
    [
      'chill',
      'lo-fi',
      'lofi',
      'ambient',
      'acoustic',
      'piano',
      'jazz',
      'downtempo',
      'sleep',
      'calm',
      'relax',
      'mellow',
    ],
  ),
  'relaxing': (
    0.25,
    0.35,
    85.0,
    ['chill', 'relax', 'calm', 'ambient', 'piano', 'acoustic', 'sleep'],
  ),
  'calm': (0.2, 0.3, 80.0, ['calm', 'relax', 'quiet', 'ambient', 'sleep']),
  'energetic': (
    0.85,
    0.75,
    130.0,
    [
      'energetic',
      'rock',
      'metal',
      'dance',
      'electronic',
      'house',
      'techno',
      'pop',
      'workout',
      'energy',
      'hype',
      'fast',
    ],
  ),
  'upbeat': (
    0.8,
    0.8,
    125.0,
    ['upbeat', 'happy', 'pop', 'dance', 'disco', 'funk', 'party'],
  ),
  'workout': (
    0.9,
    0.7,
    135.0,
    ['workout', 'metal', 'rock', 'techno', 'electronic', 'hype', 'intense'],
  ),
  'melancholic': (
    0.3,
    0.2,
    90.0,
    [
      'melancholy',
      'melancholic',
      'dark',
      'sad',
      'synthwave',
      'post-rock',
      'doom',
      'blues',
      'minor',
    ],
  ),
  'nostalgic': (
    0.4,
    0.4,
    100.0,
    ['nostalgic', 'retro', '80s', 'synthwave', 'indie', 'oldies'],
  ),
  'rainy': (
    0.25,
    0.25,
    85.0,
    ['rainy', 'jazz', 'piano', 'lo-fi', 'acoustic', 'ambient', 'blues'],
  ),
  'dark': (
    0.4,
    0.15,
    105.0,
    ['dark', 'goth', 'synthwave', 'metal', 'ambient', 'industrial'],
  ),
  'heavy': (
    0.95,
    0.6,
    140.0,
    ['heavy', 'metal', 'hardcore', 'punk', 'thrash', 'rock'],
  ),
  'euphoric': (
    0.85,
    0.85,
    128.0,
    ['euphoric', 'trance', 'dance', 'pop', 'edm'],
  ),
  'focus': (
    0.3,
    0.4,
    90.0,
    [
      'ambient',
      'classical',
      'instrumental',
      'piano',
      'soundtrack',
      'study',
      'focus',
      'work',
    ],
  ),
  'ambient': (
    0.15,
    0.3,
    75.0,
    ['ambient', 'drone', 'space', 'atmospheric', 'meditation'],
  ),
};

const _quotes = [
  (
    '"Where words fail, music speaks." — Hans Christian Andersen',
    '*"Where words fail, music speaks."*\n— **Hans Christian Andersen**',
  ),
  (
    '"Music is the shorthand of emotion." — Leo Tolstoy',
    '*"Music is the shorthand of emotion."*\n— **Leo Tolstoy**',
  ),
  (
    '"Without music, life would be a mistake." — Friedrich Nietzsche',
    '*"Without music, life would be a mistake."*\n— **Friedrich Nietzsche**',
  ),
];

const _helpSpoken =
    "I can manage your playback, curate playlists, edit the active queue, "
    "steer the music mood, query library analytics, or search online, sir. "
    "Ask me 'what can you do' or type 'help' anytime.";

const _helpDisplayed =
    '### 🤖 Jarvis Intelligence & Capability Matrix\n\n'
    '**🎵 Smart Curation & Mood Steering**\n'
    '• `play something chill` | `play something melancholic` | '
    '`play energetic workout mix` — DSP-backed acoustic mood steering\n'
    '• `play similar` | `more like this` — Walk local acoustic similarity graph\n'
    '• `play more by this artist` | `play the usual` | `surprise me` — '
    'Personal recommendations\n\n'
    '**📊 Library Analytics & Knowledge**\n'
    '• `how big is my library?` | `what are my top genres?` | '
    '`my most played tracks` — Quantitative statistics\n'
    '• `list tracks on album [Album]` | `how many tracks by [Artist]?` — '
    'Metadata lookups\n'
    '• `tell me about this track` | `what bpm is this?` — '
    'Audio trivia & DSP features\n\n'
    '**🔀 Queue Management & Playlists**\n'
    '• `add [song] to queue` | `play [song] next` — Append or insert tracks\n'
    '• `remove track [N]` | `move [song/N] to top` — Queue editing\n'
    '• `save queue as playlist [Name]` — Export current walk to playlist\n\n'
    '**🌐 Online Search & Downloads**\n'
    '• `search Qobuz for [Query]` | `search online for [Track]` — '
    'Cloud search\n'
    '• `download top match` — Fetch online track (with user confirmation)\n\n'
    '**💬 Natural Conversation & Context Memory**\n'
    '• Pronoun tracking: `play their top songs`, `queue that album`, '
    '`play it again`\n'
    '• `system status` | `what time is it` | `who made you` | '
    '`give me a quote`';

class AssistantRunner {
  AssistantRunner({
    required this.library,
    required this.player,
    AssistantRandom? random,
    DateTime Function()? now,
    Future<List<Object?>> Function()? loadHistory,
    this.llm,
  }) : random = random ?? SystemAssistantRandom(),
       now = now ?? DateTime.now,
       _loadHistory = loadHistory ?? (() async => const []);

  final AssistantLibrary library;
  final AssistantPlayer player;
  final AssistantRandom random;

  /// Local wall-clock time (`datetime.now()`); `time.time()` is read from
  /// it too.
  final DateTime Function() now;

  /// `ChatMemoryManager().load_session()["messages"]`, read when no
  /// provider was passed to [dispatch].
  final Future<List<Object?>> Function() _loadHistory;

  /// `_dispatch_llm` (stage d). Unset: the handler runs directly.
  Future<AssistantResponse> Function(Intent intent)? llm;

  final List<String> _recent = [];
  static const _recentCap = 50;
  PendingConfirmation? _pending;
  PendingChoice? _pendingChoice;
  List<Object?>? _historyCache;
  HistoryProvider? _historyProvider;

  List<String> get recent => List.unmodifiable(_recent);
  PendingConfirmation? get pending => _pending;
  PendingChoice? get pendingChoice => _pendingChoice;

  /// Stage a yes/no for the next turn; replaces any open question.
  void queueConfirmation(PendingConfirmation prompt) => _pending = prompt;

  /// Stage a multi-option choice for the next turn.
  void queueChoice(PendingChoice choice) => _pendingChoice = choice;

  /// `_say`: a random phrase of [category], formatted with [kwargs].
  String say(String category, [Map<String, Object?> kwargs = const {}]) {
    final phrase = random.choice(kJarvisPhrases[category] ?? const ['Yes?']);
    final args = Map.of(kwargs);
    if (category == 'greeting' && phrase.contains('{time_greeting}')) {
      final hour = now().hour;
      args['time_greeting'] = hour < 12
          ? 'Good morning'
          : hour < 17
          ? 'Good afternoon'
          : 'Good evening';
    }
    return pyFormat(phrase, args);
  }

  // ── Public dispatch ─────────────────────────────────────────────────────

  /// Route an Intent to its handler. A pending choice or confirmation is
  /// resolved first; anything that doesn't answer it drops it and routes
  /// as a new request. [historyProvider] returns the live chat history.
  Future<AssistantResponse> dispatch(
    Intent intent, {
    HistoryProvider? historyProvider,
  }) async {
    _historyCache = null;
    if (historyProvider != null) _historyProvider = historyProvider;
    try {
      final response = await _dispatchInner(intent);
      response.intent = intent;
      _announceAnaphora(intent, response);
      return response;
    } finally {
      _historyCache = null;
      _historyProvider = null;
    }
  }

  /// Parse and dispatch in one call.
  Future<AssistantResponse> dispatchText(
    String text, {
    HistoryProvider? historyProvider,
  }) => dispatch(parse(text), historyProvider: historyProvider);

  void _announceAnaphora(Intent intent, AssistantResponse response) {
    final label = intent.extras['anaphora_resolved_label'];
    final trigger = intent.extras['anaphora_trigger'];
    if (!pyTruthy(label) || !response.success) return;
    if (pyTruthy(trigger)) {
      response.spoken =
          "Resolving '${pyStr(trigger)}' to ${pyStr(label)}. ${response.spoken}";
    } else {
      final tail = ' (based on our recent discussion of ${pyStr(label)})';
      if (response.spoken.isNotEmpty && !response.spoken.endsWith(tail)) {
        response.spoken = '${_rstripDots(response.spoken)}.$tail';
      }
    }
  }

  static String _rstripDots(String s) {
    var end = s.length;
    while (end > 0 && s[end - 1] == '.') {
      end--;
    }
    return s.substring(0, end);
  }

  Future<AssistantResponse> _dispatchInner(Intent intent) async {
    final pendingChoice = _pendingChoice;
    if (pendingChoice != null) {
      if (intent.name == intentNegative) {
        _pendingChoice = null;
        return AssistantResponse(
          spoken: 'Understood, sir. Selection cancelled.',
          displayed: 'Selection cancelled.',
        );
      }
      ChoiceOption? selected;
      final qRaw = pyLower(
        pyStrip(_text(pyOr([intent.query, intent.raw, '']))),
      );
      final idx = _digitMap[qRaw];
      if (idx != null && idx < pendingChoice.options.length) {
        selected = pendingChoice.options[idx];
      } else {
        for (final opt in pendingChoice.options) {
          if (pyLower(opt.title).contains(qRaw) ||
              (opt.subtitle.isNotEmpty &&
                  pyLower(opt.subtitle).contains(qRaw))) {
            selected = opt;
            break;
          }
        }
      }
      if (selected != null) {
        _pendingChoice = null;
        final onSelect = pendingChoice.onSelect;
        if (onSelect != null) {
          try {
            return await onSelect(selected);
          } catch (e) {
            return AssistantResponse(
              spoken: 'I had trouble processing that choice, sir.',
              displayed: 'Error executing choice callback: $e',
              success: false,
            );
          }
        }
        return AssistantResponse(
          spoken: 'Selection confirmed: ${selected.title}.',
          displayed: 'Selected: **${selected.title}**',
        );
      }
      _pendingChoice = null;
    }

    final pending = _pending;
    if (pending != null) {
      var isYes = intent.name == intentAffirmative;
      var isNo = intent.name == intentNegative;
      if (!isYes && !isNo) {
        // Typos and natural phrasings ("yesa", "go ahead and apply it").
        final rawS = pyLower(
          pyStrip(_text(pyOr([intent.raw, intent.query, '']))),
        );
        final tokens = _wordToken.findall(rawS);
        if (tokens.isNotEmpty) {
          final first = tokens.first;
          if (_yesWords.contains(first)) {
            isYes = true;
          } else if (_noWords.contains(first)) {
            isNo = true;
          } else if (tokens.any(
                (w) => const {'apply', 'accept', 'save', 'confirm'}.contains(w),
              ) &&
              !tokens.any(
                (w) => const {'dont', 'not', 'no', 'never'}.contains(w),
              )) {
            isYes = true;
          }
        }
      }
      if (isYes) {
        _pending = null;
        final onYes = pending.onYes;
        if (onYes != null) {
          try {
            return await onYes();
          } catch (e) {
            return AssistantResponse(
              spoken: 'I had trouble processing that, sir.',
              displayed: 'Error executing callback: $e',
              success: false,
            );
          }
        }
        return AssistantResponse(
          spoken: pending.onYesMsg,
          displayed: pending.onYesMsg,
        )..action = pending.onYesAction;
      }
      if (isNo) {
        _pending = null;
        final onNo = pending.onNo;
        if (onNo != null) {
          try {
            return await onNo();
          } catch (e) {
            return AssistantResponse(
              spoken: 'I had trouble processing that, sir.',
              displayed: 'Error executing callback: $e',
              success: false,
            );
          }
        }
        return AssistantResponse(
          spoken: pending.onNoMsg,
          displayed: pending.onNoMsg,
        );
      }
      _pending = null;
    }

    final (resolved, confirmQ) = await _resolveAnaphora(intent);
    if (confirmQ != null) {
      // Ambiguity: 'no' switches to the runner-up instead of cancelling.
      final altIntent = resolved.extras.remove('anaphora_alt_intent');
      final altLabel = resolved.extras.remove('anaphora_alt_label');
      ResponseCallback? onNo;
      String noMsg;
      if (altIntent is Intent) {
        onNo = () => dispatch(altIntent);
        noMsg = pyTruthy(altLabel)
            ? 'Understood, sir. Going with **${pyStr(altLabel)}** instead.'
            : 'Understood, sir. Going with the alternative.';
      } else {
        noMsg = 'Understood, sir. Standing by.';
      }
      _pending = PendingConfirmation(
        prompt: confirmQ,
        onYesMsg: confirmQ,
        onNoMsg: noMsg,
        onYes: () => dispatch(resolved),
        onNo: onNo,
      );
      return AssistantResponse(spoken: confirmQ, displayed: confirmQ);
    }

    try {
      if (_fastIntents.contains(resolved.name)) {
        return await executeIntent(resolved);
      }
      final llm = this.llm;
      return llm != null ? await llm(resolved) : await executeIntent(resolved);
    } catch (e) {
      return AssistantResponse(
        spoken: say('error'),
        displayed: 'Error: $e',
        success: false,
      );
    }
  }

  /// `_execute_intent`: one intent through its handler, bypassing the LLM
  /// branch and the pending and anaphora machinery.
  Future<AssistantResponse> executeIntent(Intent intent) =>
      switch (intent.name) {
        intentPlayNow => _handlePlayNow(intent),
        intentQueueAdd => _handleQueueAdd(intent, next: false),
        intentQueueNext => _handleQueueAdd(intent, next: true),
        intentQueueRemove => _handleQueueRemove(intent),
        intentQueueMove => _handleQueueMove(intent),
        intentSaveQueue => _handleSaveQueue(intent),
        intentMoodSteer => _handleMoodSteer(intent),
        intentTrackInfo => _handleTrackInfo(),
        intentPlaySimilar => _handlePlaySimilar(intent),
        intentPlayMoreBy => _handlePlayMoreBy(intent),
        intentPlayRandom => _handlePlayRandom(),
        intentSkip => _handleSkip(),
        intentPrev => _handlePrev(),
        intentPause => _handlePause(),
        intentResume => _handleResume(),
        intentStop => _handleStop(),
        intentClearQueue => _handleClearQueue(),
        intentShuffle => _handleShuffle(),
        intentMute => _handleMute(),
        intentUnmute => _handleUnmute(),
        intentPlayTheUsual => _handlePlayTheUsual(),
        intentGreet => _handleGreet(),
        intentHelp => _fixed(_helpSpoken, _helpDisplayed),
        intentCreator => _fixed(
          'I am Jarvis, an assistant configured for library and audio playback '
              'management.',
          'Jarvis Audio Assistant — Configured for local library management.',
        ),
        intentCoffee => _fixed(
          'I can only assist with audio playback and library management, sir.',
          'Functionality unavailable. Jarvis is limited to audio playback and '
              'library operations.',
        ),
        intentHal => _fixed(
          'Systems are operating normally, sir. What would you like to play?',
          'Systems operational. Ready for your playback command.',
        ),
        intentIronMan => _fixed(
          'Acknowledged, sir. Systems are online and ready for your command.',
          'Systems online. Ready for playback directives.',
        ),
        intentJoke => _fixed(
          'I am better equipped for music curation than comedy, sir, but I am '
              'happy to manage your queue.',
          'I am better equipped for music curation than comedy, sir, but I am '
              'happy to manage your queue.',
        ),
        intentTimeDate => _handleTimeDate(),
        intentStatus => _fixed(
          'All systems nominal, sir. Audio decoders operating at peak '
              'performance and music graph matrix is standing by.',
          '**System Diagnostics**\n• Core Engine: Operational\n'
              '• Audio Service: Active\n• Database Index: Synced',
        ),
        intentThanks => _fixed(
          'You are very welcome, sir.',
          'You are very welcome, sir.',
        ),
        intentQuote => _handleQuote(),
        // choice_select, affirmative, negative, name_entity and unknown.
        _ => _handleUnknown(intent),
      };

  Future<AssistantResponse> _fixed(String spoken, String displayed) async =>
      AssistantResponse(spoken: spoken, displayed: displayed);

  // ── Recent playback ─────────────────────────────────────────────────────

  /// `_remember`: the in-session avoid list, plus a fire-and-forget
  /// `played` row for the long-term one.
  void _remember(Object? path, {String? seedPath}) {
    if (path is! String || path.isEmpty) return;
    _recent
      ..remove(path)
      ..add(path);
    if (_recent.length > _recentCap) _recent.removeAt(0);
    unawaited(
      library
          .recordPlayback(path, 'played', seedPath: seedPath)
          .catchError((Object _) {}),
    );
  }

  /// `_avoid_set`: the session list and the last 7 days of plays.
  Future<Set<String>> _avoidSet() async {
    final out = {..._recent};
    try {
      out.addAll(await library.recentPlayedPaths(windowSeconds: 7 * 86400));
    } catch (_) {
      // Fresh installs: persistent history is an optimisation.
    }
    return out;
  }

  // ── Resolution ──────────────────────────────────────────────────────────

  Future<Row?> _resolveQuery(String query) async {
    if (query.isEmpty) return null;
    final hits = await _resolveQueries(query, limit: 1);
    return hits.isEmpty ? null : hits.first;
  }

  /// `_resolve_queries`: the library search, then a fuzzy title/artist
  /// match (difflib ratio above 0.65) over the whole library.
  Future<List<Row>> _resolveQueries(String query, {int limit = 25}) async {
    if (query.isEmpty) return [];
    final hits = await library.searchTracksSimple(query, limit: limit);
    if (hits.isNotEmpty) {
      for (final h in hits) {
        h['fuzzy_match'] = false;
      }
      return hits;
    }
    final all = await library.getAllTracks();
    if (all.isEmpty) return [];
    final qLower = pyLower(query);
    final scored = <(double, Row)>[];
    for (final t in all) {
      final title = pyLower(_text(t['title']));
      final artist = pyLower(_text(t['artist']));
      final combined = '$title $artist';
      final score = [
        pySequenceRatio(qLower, title),
        pySequenceRatio(qLower, artist),
        pySequenceRatio(qLower, combined),
      ].reduce(math.max);
      if (score > 0.65) scored.add((score, t));
    }
    if (scored.isEmpty) return [];
    final ranked = _stableSorted(scored, (x, y) => y.$1.compareTo(x.$1));
    return [
      for (final (_, t) in ranked.take(limit))
        {
          'path': t['path'],
          'title': pyOr([t['title'], t['track_title'], '']),
          'artist': pyOr([t['artist'], t['artist_name'], 'Unknown Artist']),
          'album': pyOr([t['album'], t['album_title'], 'Unknown Album']),
          'duration': pyOr([t['duration'], 0.0]),
          'image_url': pyOr([t['image_url'], '']),
          'fuzzy_match': true,
        },
    ];
  }

  // ── Anaphora ────────────────────────────────────────────────────────────

  /// `_resolve_anaphora`: the pronoun is a hard type filter over the
  /// entities stored with recent replies; the best candidate rewrites the
  /// intent. Returns a confirmation question instead of acting when two
  /// candidates are close or the winner is far back.
  Future<(Intent, String?)> _resolveAnaphora(Intent intent) async {
    final queryLower = pyLower(pyStrip(intent.query ?? ''));
    final m = _anaphoraRe.search(queryLower);
    final trigger = m == null
        ? null
        : _spaces.sub(pyLower(pyStrip(m.text)), ' ');
    var hintType = trigger == null ? null : pronounType[trigger];
    final needsContext =
        _implicitAnaphoraIntents.contains(intent.name) &&
        player.currentPath.isEmpty;
    if (trigger == null && !needsContext) return (intent, null);
    if (hintType == null && needsContext) {
      hintType = _intentDefaultType[intent.name];
    }

    List<Object?> messages;
    try {
      final cached = _historyCache;
      if (cached != null) {
        messages = cached;
      } else if (_historyProvider != null) {
        messages = List.of(_historyProvider!());
        _historyCache = messages;
      } else {
        messages = await _loadHistory();
        _historyCache = messages;
      }
    } catch (_) {
      return (intent, null);
    }
    if (messages.isEmpty) return (intent, null);

    // Skip the current user turn (appended before dispatch).
    var scan = messages.reversed.take(_anaphoraScanWindow).toList();
    if (scan.isNotEmpty && pyGet(scan.first, 'sender') == 'user') {
      scan = scan.sublist(1);
    }

    final candidates = <_Candidate>[];
    for (final (rank, msg) in scan.indexed) {
      final message = msg! as Map<String, Object?>;
      if (message['sender'] != 'assistant') continue;
      final entities = message['entities'];
      if (pyTruthy(entities)) {
        final e = entities! as Map<String, Object?>;
        final src = e['intent'];
        final track = e['track'];
        if (pyTruthy(track) && pyTruthy(pyGet(track, 'path'))) {
          candidates.add((rank, 'track', track, src));
        }
        final artist = e['artist'];
        if (pyTruthy(artist)) candidates.add((rank, 'artist', artist, src));
        final album = e['album'];
        if (pyTruthy(album)) candidates.add((rank, 'album', album, src));
      } else {
        // Messages from before entities: the bold markdown.
        final text = _getOr(message, 'text', '')! as String;
        String? legTitle, legArtist;
        for (final hit in _bold.findall(text)) {
          if (hit.contains(' — ') || hit.contains(' - ')) {
            final sep = hit.contains(' — ') ? ' — ' : ' - ';
            final at = hit.indexOf(sep);
            final p0 = pyStrip(hit.substring(0, at));
            final p1 = pyStrip(hit.substring(at + sep.length));
            legTitle = pyTruthy(legTitle) ? legTitle : p0;
            legArtist = pyTruthy(legArtist) ? legArtist : p1;
          } else if (pyTruthy(legTitle) && !pyTruthy(legArtist)) {
            legArtist = pyStrip(hit);
          } else {
            legTitle = pyTruthy(legTitle) ? legTitle : pyStrip(hit);
          }
        }
        if (pyTruthy(legTitle)) {
          candidates.add((rank, 'track_legacy', (legTitle!, legArtist), null));
        } else if (pyTruthy(legArtist)) {
          candidates.add((rank, 'artist_legacy', legArtist, null));
        }
      }
    }

    final pool = switch (hintType) {
      'track' => [
        for (final c in candidates)
          if (c.$2 == 'track' || c.$2 == 'track_legacy') c,
      ],
      'artist' => [
        for (final c in candidates)
          if (c.$2 == 'artist' || c.$2 == 'artist_legacy') c,
      ],
      'album' => [
        for (final c in candidates)
          if (c.$2 == 'album') c,
      ],
      _ => <_Candidate>[],
    };
    if (pool.isEmpty) return (intent, null);

    double score(_Candidate c) {
      final src = pyTruthy(c.$4) ? c.$4 : '';
      final weight =
          (src is String ? _intentWeight[src] : null) ?? _intentDefaultWeight;
      return -c.$1 + weight * _anaphoraIntentK;
    }

    final sortedPool = _stableSorted(pool, (x, y) {
      final c = score(y).compareTo(score(x));
      return c != 0 ? c : x.$1 - y.$1;
    });
    final best = sortedPool.first;
    intent.extras['resolved_via_intent'] = pyTruthy(best.$4) ? best.$4 : '';

    final primary = await _applyAnaphoraResolution(intent, best, needsContext);
    if (primary == null) return (intent, null);
    intent.extras['anaphora_resolved_label'] = primary;
    intent.extras['anaphora_trigger'] = trigger ?? '';

    // Never pick silently between two equally recent entities.
    Intent? altIntent;
    String? altLabel;
    if (sortedPool.length >= 2) {
      final alt = sortedPool[1];
      if (alt.$1 - best.$1 <= _anaphoraAmbiguityGap) {
        altIntent = Intent(
          intent.name,
          query: intent.raw.isNotEmpty ? intent.raw : queryLower,
          raw: intent.raw,
          extras: {
            for (final e in intent.extras.entries)
              if (!const {
                'resolved_track',
                'seed_path_override',
                'seed_artist_override',
              }.contains(e.key))
                e.key: e.value,
          },
        );
        final label = await _applyAnaphoraResolution(
          altIntent,
          alt,
          needsContext,
        );
        if (label == null || pyLower(label) == pyLower(primary)) {
          altIntent = null;
        } else {
          altLabel = label;
        }
      }
    }

    String? confirmQ;
    if (altIntent != null && altLabel != null) {
      confirmQ =
          'Did you mean **$primary**, sir? '
          "(say 'no' for **$altLabel** instead)";
      intent.extras['anaphora_alt_intent'] = altIntent;
      intent.extras['anaphora_alt_label'] = altLabel;
    } else if (best.$1 > _anaphoraConfirmThreshold) {
      confirmQ = 'Just to confirm, sir — did you mean **$primary**?';
    }
    return (intent, confirmQ);
  }

  /// `_apply_anaphora_resolution`: resolve [candidate] to a track or an
  /// artist and rewrite [target]; returns its label, or null.
  Future<String?> _applyAnaphoraResolution(
    Intent target,
    _Candidate candidate,
    bool needsContext,
  ) async {
    final (_, type, data, _) = candidate;
    Row? track;
    Object? artist;
    if (type == 'track') {
      track = data! as Row;
    } else if (type == 'track_legacy') {
      final (title, art) = data! as (String, String?);
      final q = pyTruthy(art) ? pyStrip('$title $art') : title;
      try {
        track = await _resolveQuery(q);
      } catch (_) {}
    } else if (type == 'artist') {
      artist = data;
    } else if (type == 'artist_legacy') {
      try {
        final name = data! as String;
        final names = await library.searchArtistNames(name);
        if (names.isNotEmpty) {
          final target = pyLower(name);
          artist = names.firstWhere(
            (a) => pyLower(a) == target,
            orElse: () => names.first,
          );
        }
      } catch (_) {}
    }
    if (track == null && artist == null) return null;

    if (track != null) {
      final title = pyOr([track['title'], track['track_title'], '']);
      final art = pyOr([track['artist'], track['artist_name'], '']);
      target.query = pyStrip('${pyStr(title)} ${pyStr(art)}');
      target.extras['resolved_track'] = track;
      if (needsContext && player.currentPath.isEmpty) {
        final seed = pyOr([track['path'], '']);
        if (pyTruthy(seed)) {
          target.extras['seed_path_override'] = seed;
          target.extras['seed_artist_override'] = art;
        }
      }
      return pyTruthy(art)
          ? '${pyStr(title)} by ${pyStr(art)}'
          : (pyTruthy(title) ? pyStr(title) : 'that track');
    }

    final name = pyStr(artist);
    target.query = name;
    if (needsContext && player.currentPath.isEmpty) {
      try {
        final path = await library.firstTrackPathByArtist(name);
        if (path != null) {
          target.extras['seed_path_override'] = path;
          target.extras['seed_artist_override'] = artist;
        }
      } catch (_) {}
    }
    return name;
  }

  // ── Handlers ────────────────────────────────────────────────────────────

  Future<AssistantResponse> _handlePlayNow(Intent intent) async {
    // One match plays; several are queued from a random start.
    final resolved = intent.extras['resolved_track'];
    final hits = pyTruthy(resolved)
        ? [resolved! as Row]
        : await _resolveQueries(intent.query ?? '');
    final q = pyStr(intent.query);
    if (hits.isEmpty) {
      return AssistantResponse(
        spoken: "I couldn't find anything matching '$q', sir.",
        displayed: 'No local match for **$q**.',
        success: false,
      );
    }
    final engineTracks = [for (final t in hits) toEngineTrack(t)];
    var first = hits.first;
    final isFuzzy = pyTruthy(_getOr(first, 'fuzzy_match', false));
    final prefixSpoken = isFuzzy
        ? "I couldn't find exactly '$q', sir. Playing "
        : '';
    final prefixDisplayed = isFuzzy ? "No exact match for '$q'. Playing " : '';

    // Several hits whose titles match, by two or more artists: ask.
    final qClean = pyLower(pyStrip(intent.query ?? ''));
    if (hits.length > 1 && qClean.isNotEmpty) {
      final titleMatches = [
        for (final h in hits)
          if (pyLower(
            _text(pyOr([h['title'], h['track_title'], ''])),
          ).contains(qClean))
            h,
      ];
      final distinctArtists = {
        for (final h in titleMatches)
          pyLower(_text(pyOr([h['artist'], h['artist_name'], '']))),
      };
      if (titleMatches.length >= 2 && distinctArtists.length >= 2) {
        final opts = [
          for (final (i, h) in titleMatches.take(4).indexed)
            ChoiceOption(
              id: '${i + 1}',
              title: '${pyStr(h['title'])} — ${pyStr(h['artist'])}',
              subtitle: 'Album: ${pyStr(_getOr(h, 'album', 'Unknown Album'))}',
              payload: h,
            ),
        ];
        Future<AssistantResponse> onSelect(ChoiceOption opt) async {
          final t = opt.payload;
          final et = toEngineTrack(t);
          player.setQueue([et]);
          _remember(et['path']);
          return AssistantResponse(
            spoken:
                "${say('affirmative')} Playing '${pyStr(t['title'])}' by "
                '${pyStr(t['artist'])}.',
            displayed:
                'Playing choice: **${pyStr(t['title'])}** — '
                '${pyStr(t['artist'])}',
            extras: {'track': t},
            deferredPlay: true,
          );
        }

        final disSpoken = say('disambiguation', {
          'count': opts.length,
          'query': intent.query,
        });
        queueChoice(
          PendingChoice(prompt: disSpoken, options: opts, onSelect: onSelect),
        );
        return AssistantResponse(
          spoken: disSpoken,
          displayed: 'Multiple matches found for **$q**. Please choose one:',
          extras: {
            'options': [
              for (final o in opts)
                {'id': o.id, 'title': o.title, 'subtitle': o.subtitle},
            ],
          },
        );
      }
    }

    if (engineTracks.length == 1) {
      player.setQueue(engineTracks);
      _remember(engineTracks.first['path']);
      final album = _text(pyOr([first['album'], first['album_title'], '']));
      final albumStr = album.isNotEmpty ? " from the album '$album'" : '';
      final dur = _durationSuffix(first['duration']);
      return AssistantResponse(
        spoken:
            "${say('affirmative')} ${prefixSpoken}playing "
            "'${pyStr(first['title'])}' by ${pyStr(first['artist'])}"
            '$albumStr$dur.',
        displayed:
            'Certainly. $prefixDisplayed**${pyStr(first['title'])}** — '
            '${pyStr(first['artist'])}$albumStr$dur',
        extras: {'track': first},
        deferredPlay: true,
      );
    }

    // A random opener, so repeated 'play radiohead' varies.
    final start = random.randint(0, engineTracks.length - 1);
    player.setQueue(engineTracks, startIndex: start);
    _remember(engineTracks[start]['path']);
    first = hits[start];
    final album = _text(pyOr([first['album'], first['album_title'], '']));
    final albumStr = album.isNotEmpty ? " from the album '$album'" : '';
    final dur = _durationSuffix(first['duration']);
    return AssistantResponse(
      spoken:
          "${say('affirmative')} $prefixSpoken${engineTracks.length} tracks "
          "matching '$q'. Starting with '${pyStr(first['title'])}' by "
          '${pyStr(first['artist'])}$albumStr$dur.',
      displayed:
          'Queued **${engineTracks.length}** matches for **$q**. '
          '$prefixDisplayed**${pyStr(first['title'])}** — '
          '${pyStr(first['artist'])}$albumStr$dur.',
      extras: {
        'queued': engineTracks.length,
        'first': first,
        'is_multi': true,
        'query': intent.query ?? '',
      },
      deferredPlay: true,
    );
  }

  Future<AssistantResponse> _handlePlayRandom() async {
    final tracks = await library.getAllTracks();
    if (tracks.isEmpty) {
      return AssistantResponse(
        spoken:
            'Your library is currently empty, sir. Please configure your '
            'music folder first.',
        displayed: 'Library is empty — cannot play a random track.',
        success: false,
      );
    }
    final engineTracks = [for (final t in tracks) toEngineTrack(t)];
    random.shuffle(engineTracks);
    player
      ..isShuffle = true
      ..setQueue(engineTracks);
    _remember(engineTracks.first['path']);
    final first = engineTracks.first;
    final title = pyStr(pyOr([first['track_title'], 'Unknown Track']));
    final artist = pyStr(pyOr([first['artist_name'], 'Unknown Artist']));
    return AssistantResponse(
      spoken:
          '${say('affirmative')} Initiating shuffle play. Starting with '
          '$title by $artist.',
      displayed:
          'Shuffle play active. Queued **${engineTracks.length}** tracks. '
          'Starting with: **$title** — $artist',
      extras: {
        'track': first,
        'queued': engineTracks.length,
        'entity_intent': 'play_random',
      },
      deferredPlay: true,
    );
  }

  /// `_handle_queue_add` and `_handle_queue_next`.
  Future<AssistantResponse> _handleQueueAdd(
    Intent intent, {
    required bool next,
  }) async {
    final resolved = intent.extras['resolved_track'];
    final track = pyTruthy(resolved)
        ? resolved! as Row
        : await _resolveQuery(intent.query ?? '');
    final q = pyStr(intent.query);
    if (track == null) {
      return AssistantResponse(
        spoken: "I couldn't find a track matching '$q'.",
        displayed: 'No local match for **$q**.',
        success: false,
      );
    }
    final et = toEngineTrack(track);
    String verb;
    if (player.queue.isEmpty) {
      player.setQueue([et]);
      verb = 'Playing';
    } else if (next) {
      player.queueNext(et);
      verb = 'Playing next';
    } else {
      player.queueLast(et);
      verb = 'Added to queue';
    }
    final album = _text(pyOr([track['album'], track['album_title'], '']));
    final albumStr = album.isNotEmpty ? " from the album '$album'" : '';
    final dur = _durationSuffix(track['duration']);
    final qLen = player.queue.length;
    final suffixSpoken = qLen > 1 ? ' The queue now has $qLen tracks.' : '';
    return AssistantResponse(
      spoken:
          "${say('affirmative')} $verb: '${pyStr(track['title'])}' by "
          '${pyStr(track['artist'])}$albumStr$dur.$suffixSpoken',
      displayed:
          '$verb: **${pyStr(track['title'])}** — ${pyStr(track['artist'])}'
          '$albumStr$dur. [Queue size: $qLen]',
      extras: {'track': track},
    );
  }

  Future<AssistantResponse> _handlePlaySimilar(Intent intent) async {
    final override = intent.extras['seed_path_override'];
    final seed = pyTruthy(override) ? override! as String : player.currentPath;
    if (seed.isEmpty) {
      return AssistantResponse(
        spoken: 'Nothing is playing right now.',
        displayed:
            'No current track — start something first, then ask for '
            'similar tracks.',
        success: false,
      );
    }
    // Only the seed needs features: unanalysed tracks aren't candidates.
    bool analysed;
    try {
      analysed = await library.hasCurrentFeatures(seed);
    } catch (_) {
      analysed = true;
    }
    if (!analysed) {
      return AssistantResponse(
        spoken:
            "This track isn't analysed yet, sir, so I can't find its "
            'neighbours.',
        displayed:
            'This track lacks DSP features. Run the Jarvis analyzer, or pick '
            'an analysed track.',
        success: false,
      );
    }
    final avoid = await _avoidSet()
      ..add(seed);
    List<String> walkPaths;
    try {
      walkPaths = await library.walk(seed, length: 12, avoid: avoid);
    } catch (_) {
      walkPaths = [];
    }
    if (walkPaths.isEmpty) {
      return AssistantResponse(
        spoken: "I don't have enough information to find similar tracks yet.",
        displayed:
            "The music graph hasn't been built for this track yet. Open "
            'Settings → Permissions to enable file access, then re-run the '
            'assistant initialisation.',
        success: false,
      );
    }
    final engineTracks = <Row>[];
    for (final p in walkPaths) {
      final row = await library.getTrackFull(p);
      if (row == null) continue;
      engineTracks.add(toEngineTrack(row));
    }
    final verb = intent.extras['verb'];
    final isQueue =
        verb is String &&
        verb.isNotEmpty &&
        const {
          'add',
          'queue',
          'enqueue',
          'put',
        }.contains(pyStrip(pyLower(verb)));

    final firstRow = await library.getTrackFull(walkPaths.first);
    final firstName = firstRow != null
        ? '${pyStr(firstRow['title'])} — ${pyStr(firstRow['artist'])}'
        : 'a similar track';
    final seedRow = await library.getTrackFull(seed);
    final seedName = seedRow != null
        ? "'${pyStr(seedRow['title'])}' by ${pyStr(seedRow['artist'])}"
        : 'the active track';

    if (isQueue && player.queue.isNotEmpty) {
      // Add to Queue, ahead of auto-play's recommendations.
      player.queueLastMany(engineTracks);
      for (final t in engineTracks) {
        _remember(t['path'], seedPath: seed);
      }
      final added = engineTracks.length;
      return AssistantResponse(
        spoken: "I've added $added tracks similar to $seedName to the queue.",
        displayed:
            'Similarity sequence initiated based on **$seedName**. Queued '
            '**$added** tracks (via acoustic walk). Next similar: '
            '**$firstName**.',
        extras: {
          'added': added,
          'kind': 'walk',
          'entity_intent': 'play_similar_bulk',
        },
      );
    }
    player.setQueue(engineTracks);
    var added = 0;
    for (final t in engineTracks) {
      _remember(t['path'], seedPath: seed);
      added++;
    }
    return AssistantResponse(
      spoken:
          "${say('discovery')} Playing tracks similar to $seedName. Starting "
          'with $firstName.',
      displayed:
          'Similarity sequence initiated based on **$seedName**. Now playing '
          '**$added** tracks (via acoustic walk). First similar: '
          '**$firstName**.',
      extras: {
        'added': added,
        'kind': 'walk',
        'entity_intent': 'play_similar_bulk',
      },
      deferredPlay: true,
    );
  }

  Future<AssistantResponse> _handlePlayMoreBy(Intent intent) async {
    final override = intent.extras['seed_path_override'];
    final seed = pyTruthy(override) ? override! as String : player.currentPath;
    if (seed.isEmpty) {
      return AssistantResponse(
        spoken: 'Nothing is playing right now.',
        displayed: 'No current track — pick a song first.',
        success: false,
      );
    }
    final nbrs = await library.artistNeighbors(seed, k: 10);
    if (nbrs.isEmpty) {
      return AssistantResponse(
        spoken: "I don't have other tracks by this artist in your library.",
        displayed: 'No other tracks by this artist were found locally.',
        success: false,
      );
    }
    final avoid = await _avoidSet();
    var added = 0;
    for (final p in nbrs) {
      if (avoid.contains(p)) continue;
      final row = await library.getTrackFull(p);
      if (row == null) continue;
      player.queueLast(toEngineTrack(row));
      _remember(p);
      added++;
      if (added >= 5) break;
    }
    var artistName = pyOr([
      intent.extras['seed_artist_override'],
      player.currentArtist,
    ]);
    if (!pyTruthy(artistName)) {
      final seedRow = await library.getTrackFull(seed);
      if (seedRow != null) artistName = seedRow['artist'];
    }
    final name = pyStr(pyOr([artistName, 'this artist']));
    return AssistantResponse(
      spoken: '${say('affirmative')} Queued $added more tracks by $name.',
      displayed: 'Queued **$added** more tracks by **$name**.',
      extras: {'added': added},
    );
  }

  Future<AssistantResponse> _handleSkip() async {
    if (player.queue.isEmpty) {
      return AssistantResponse(
        spoken: 'The queue is empty.',
        displayed: 'Nothing to skip — queue is empty.',
        success: false,
      );
    }
    player.next();
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Skipping'}),
      displayed: 'Skipped.',
    );
  }

  Future<AssistantResponse> _handlePrev() async {
    if (player.queue.isEmpty) {
      return AssistantResponse(
        spoken: 'The queue is empty.',
        displayed: 'Nothing to go back to — queue is empty.',
        success: false,
      );
    }
    player.previous();
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Going back'}),
      displayed: 'Previous track.',
    );
  }

  Future<AssistantResponse> _handlePause() async {
    if (!player.isPlaying) {
      return AssistantResponse(
        spoken: 'Already paused.',
        displayed: 'Already paused.',
      );
    }
    player.pause();
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Pausing playback'}),
      displayed: 'Paused.',
    );
  }

  Future<AssistantResponse> _handleResume() async {
    if (player.isPlaying) {
      return AssistantResponse(
        spoken: 'Already playing.',
        displayed: 'Already playing.',
      );
    }
    player.play();
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Resuming playback'}),
      displayed: 'Resuming playback.',
    );
  }

  Future<AssistantResponse> _handleStop() async {
    player.stop();
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Stopping the music'}),
      displayed: 'Stopped.',
    );
  }

  Future<AssistantResponse> _handleClearQueue() async {
    player.clearQueue();
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Queue cleared'}),
      displayed: 'Queue cleared.',
    );
  }

  Future<AssistantResponse> _handleShuffle() async {
    final on = !player.isShuffle;
    player.isShuffle = on;
    final verb = on ? 'on' : 'off';
    return AssistantResponse(
      spoken: say('playback_control', {'action': 'Shuffle $verb'}),
      displayed: 'Shuffle $verb.',
    );
  }

  Future<AssistantResponse> _handleMute() async {
    // Mute is a pause: the engine has no volume-0 hook.
    player.pause();
    return AssistantResponse(
      spoken: '${say('affirmative')} Silencing the output.',
      displayed: 'Audio muted (paused), sir.',
    );
  }

  Future<AssistantResponse> _handleUnmute() async {
    player.play();
    return AssistantResponse(
      spoken: '${say('affirmative')} Restoring audio output.',
      displayed: 'Audio unmuted (resumed), sir.',
    );
  }

  Future<List<Row>> _mostPlayed() async {
    try {
      return await library.getMostPlayed(limit: 20);
    } catch (_) {
      return [];
    }
  }

  Future<AssistantResponse> _handleGreet() async {
    final tracks = await _mostPlayed();
    if (tracks.isNotEmpty && random.random() < 0.5) {
      Future<AssistantResponse> playTheUsual() async {
        final track = random.choice(tracks);
        final et = toEngineTrack(track);
        player.setQueue([et]);
        _remember(et['path']);
        final title = pyStr(pyOr([track['title'], 'Unknown Song']));
        final artist = pyStr(pyOr([track['artist'], 'Unknown Artist']));
        return AssistantResponse(
          spoken:
              'Very good, sir. Queuing up $title by $artist, one of your '
              'favorites.',
          displayed: 'Playing the usual: **$title** — $artist',
          deferredPlay: true,
        );
      }

      const prompt = 'Good to see you, sir. Shall I queue up the usual?';
      queueConfirmation(
        PendingConfirmation(
          prompt: prompt,
          onYes: playTheUsual,
          onNoMsg: 'Understood. Let me know if you need anything, sir.',
        ),
      );
      return AssistantResponse(spoken: prompt, displayed: prompt);
    }
    final phrase = say('greeting');
    return AssistantResponse(spoken: phrase, displayed: phrase);
  }

  Future<AssistantResponse> _handlePlayTheUsual() async {
    final tracks = await _mostPlayed();
    if (tracks.isEmpty) {
      return AssistantResponse(
        spoken:
            "I don't have enough play history to know what your usual is, "
            'sir.',
        displayed: 'No play history found — play count data is empty.',
        success: false,
      );
    }
    final track = random.choice(tracks);
    final et = toEngineTrack(track);
    player.setQueue([et]);
    _remember(et['path']);
    final title = pyStr(pyOr([track['title'], 'Unknown Song']));
    final artist = pyStr(pyOr([track['artist'], 'Unknown Artist']));
    final album = _text(pyOr([track['album'], track['album_title'], '']));
    final albumStr = album.isNotEmpty ? " from the album '$album'" : '';
    final dur = _durationSuffix(track['duration']);
    return AssistantResponse(
      spoken:
          'Very good, sir. Queuing up $title by $artist$albumStr$dur, one of '
          'your favorites.',
      displayed: 'Playing the usual: **$title** — $artist$albumStr$dur',
      deferredPlay: true,
    );
  }

  /// The last resort: the whole utterance as a library search.
  Future<AssistantResponse> _handleUnknown(Intent intent) async {
    final text = pyStrip(intent.raw);
    if (text.isEmpty) {
      return AssistantResponse(
        spoken: "I didn't catch that.",
        displayed: '(empty input)',
        success: false,
      );
    }
    final track = await _resolveQuery(text);
    if (track == null) {
      return AssistantResponse(
        spoken: say('unknown'),
        displayed:
            "I didn't understand that. Try 'help' to see my "
            'capabilities.',
        success: false,
      );
    }
    final et = toEngineTrack(track);
    player.setQueue([et]);
    _remember(et['path']);
    final isFuzzy = pyTruthy(_getOr(track, 'fuzzy_match', false));
    final prefixSpoken = isFuzzy
        ? "I couldn't find exactly '$text', sir. Playing "
        : 'Playing ';
    final prefixDisplayed = isFuzzy
        ? 'No exact match. Best guess: playing '
        : 'Best guess: playing ';
    return AssistantResponse(
      spoken:
          '$prefixSpoken${pyStr(track['title'])} by ${pyStr(track['artist'])}.',
      displayed:
          '$prefixDisplayed**${pyStr(track['title'])}** — '
          '${pyStr(track['artist'])}',
      extras: {'track': track, 'fallback': true},
      deferredPlay: true,
    );
  }

  Future<AssistantResponse> _handleTimeDate() async {
    final t = now();
    final h12 = t.hour % 12 == 0 ? 12 : t.hour % 12;
    // strftime("%I:%M %p").lstrip("0"): '09:05 AM' → '9:05 AM'.
    var time = '${_two(h12)}:${_two(t.minute)} ${t.hour < 12 ? 'AM' : 'PM'}';
    while (time.startsWith('0')) {
      time = time.substring(1);
    }
    final date =
        '${_weekdays[t.weekday - 1]}, ${_months[t.month - 1]} '
        '${_two(t.day)}, ${t.year}';
    return AssistantResponse(
      spoken: 'It is currently $time on $date, sir.',
      displayed: 'Time: **$time** | Date: **$date**',
    );
  }

  Future<AssistantResponse> _handleQuote() async {
    final (spoken, displayed) = random.choice(_quotes);
    return AssistantResponse(spoken: spoken, displayed: displayed);
  }

  // ── Queue editing ───────────────────────────────────────────────────────

  /// `if q.isdigit(): n = int(q) - 1`; `int()` refuses the superscripts and
  /// circled digits `isdigit` accepts.
  static int? _queueNumber(String q) {
    if (!pyIsDigit(q)) return null;
    if (!pyIsDecimal(q)) {
      throw PyValueError("invalid literal for int() with base 10: '$q'");
    }
    return pyDecimalInt(q) - 1;
  }

  static int? _titleIndex(List<Row> queue, String q) {
    for (final (i, tr) in queue.indexed) {
      if (pyLower(
        _text(pyOr([tr['track_title'], tr['title'], ''])),
      ).contains(q)) {
        return i;
      }
    }
    return null;
  }

  Future<AssistantResponse> _handleQueueRemove(Intent intent) async {
    if (player.queue.isEmpty) {
      return AssistantResponse(
        spoken: 'The queue is currently empty, sir.',
        displayed: 'Queue is empty.',
        success: false,
      );
    }
    final q = pyLower(pyStrip(intent.query ?? ''));
    int? idx;
    final n = _queueNumber(q);
    if (n != null && n >= 0 && n < player.queue.length) idx = n;
    if (idx == null) {
      if (const {
        'last',
        'the last song',
        'the last track',
        'last track',
      }.contains(q)) {
        idx = player.queue.length - 1;
      } else if (const {
        'first',
        'the first song',
        'the first track',
      }.contains(q)) {
        idx = 0;
      } else {
        idx = _titleIndex(player.queue, q);
      }
    }
    final removed = idx != null ? player.queue[idx] : null;
    // Through the engine (native removal and the index shift).
    if (removed != null) player.removeFromQueue(idx!);
    final qs = pyStr(intent.query);
    if (!pyTruthy(removed)) {
      return AssistantResponse(
        spoken: "I couldn't find '$qs' in the current queue, sir.",
        displayed: 'Item **$qs** not found in active queue.',
        success: false,
      );
    }
    final title = pyStr(
      pyOr([removed!['track_title'], removed['title'], 'Track']),
    );
    final artist = pyStr(
      pyOr([removed['artist_name'], removed['artist'], 'Unknown Artist']),
    );
    final remaining = player.queue.length;
    final affirmative = say('affirmative');
    return AssistantResponse(
      spoken: say('queue_remove', {
        'title': title,
        'artist': artist,
        'remaining': remaining,
        'affirmative': affirmative,
      }),
      displayed:
          'Removed: **$title** — $artist. Queue remaining: **$remaining**.',
    );
  }

  Future<AssistantResponse> _handleQueueMove(Intent intent) async {
    if (player.queue.length < 2) {
      return AssistantResponse(
        spoken: 'The queue does not have enough tracks to reorder, sir.',
        displayed: 'Not enough tracks in queue to reorder.',
        success: false,
      );
    }
    final q = pyLower(pyStrip(intent.query ?? ''));
    int? target;
    final n = _queueNumber(q);
    if (n != null && n >= 0 && n < player.queue.length) target = n;
    target ??= _titleIndex(player.queue, q);
    final qs = pyStr(intent.query);
    if (target == null) {
      return AssistantResponse(
        spoken: "I couldn't find '$qs' in the active queue, sir.",
        displayed: 'Item **$qs** not found in queue.',
        success: false,
      );
    }
    final track = player.queue[target];
    final cur = player.currentIndex;
    // Right after the playing row; removing a row above it shifts it up.
    final insertAt = target < cur
        ? cur
        : math.min(cur + 1, player.queue.length - 1);
    if (target != cur) player.moveQueueItem(target, insertAt);
    final title = pyStr(pyOr([track['track_title'], track['title'], 'Track']));
    final affirmative = say('affirmative');
    return AssistantResponse(
      spoken: say('queue_move', {
        'title': title,
        'pos': insertAt + 1,
        'affirmative': affirmative,
      }),
      displayed: 'Moved **$title** to position #${insertAt + 1} in queue.',
    );
  }

  Future<AssistantResponse> _handleSaveQueue(Intent intent) async {
    if (player.queue.isEmpty) {
      return AssistantResponse(
        spoken: 'There are no active walk tracks in your queue, sir.',
        displayed: 'Queue is empty. Cannot save walk.',
        success: false,
      );
    }
    var name = pyStrip(_text(pyOr([intent.query, 'Saved Walk'])));
    int playlistId;
    try {
      playlistId = await library.createPlaylist(name);
    } catch (_) {
      name = '$name (${now().millisecondsSinceEpoch ~/ 1000})';
      playlistId = await library.createPlaylist(name);
    }
    // The walk from the playing row on.
    var cur = player.currentIndex;
    if (cur < 0 || cur >= player.queue.length) cur = 0;
    var added = 0;
    for (final track in player.queue.sublist(cur)) {
      final path = track['path'];
      if (pyTruthy(path)) {
        await library.addTrackToPlaylist(playlistId, path! as String);
        added++;
      }
    }
    final affirmative = say('affirmative');
    return AssistantResponse(
      spoken: say('queue_save', {
        'name': name,
        'count': added,
        'affirmative': affirmative,
      }),
      displayed: 'Saved current walk to playlist: **$name** ($added tracks).',
    );
  }

  Future<AssistantResponse> _handleMoodSteer(Intent intent) async {
    final mood = pyStrip(pyLower(_text(pyOr([intent.query, 'chill']))));
    final tracks = await library.getAllTracks();
    if (tracks.isEmpty) {
      return AssistantResponse(
        spoken: 'Your library is currently empty, sir.',
        displayed: 'Library empty.',
        success: false,
      );
    }
    (double, double, double, List<String>)? profile;
    for (final e in _moodProfiles.entries) {
      if (mood.contains(e.key)) {
        profile = e.value;
        break;
      }
    }
    final (targetE, targetB, targetBpm, kws) =
        profile ?? (0.5, 0.5, 110.0, [mood]);

    double toD(Object? v) => (v! as num).toDouble();
    double moodScore(Row t) {
      var score = 0.0;
      final bpm = toD(pyOr([t['bpm'], t['tempo'], 0.0]));
      final energy = toD(pyOr([_getOr(t, 'energy', 0.5), 0.5]));
      final brightness = toD(pyOr([_getOr(t, 'brightness', 0.5), 0.5]));
      final eDist = (energy - targetE).abs();
      final bDist = (brightness - targetB).abs();
      final bpmDist = bpm > 0 ? (bpm - targetBpm).abs() / 100.0 : 0.5;
      final penalty = (eDist * 4.0) + (bDist * 3.0) + (bpmDist * 2.0);
      score += math.max(0.0, 10.0 - penalty);
      final genre = pyLower(_text(t['genre']));
      final title = pyLower(_text(pyOr([t['title'], t['track_title'], ''])));
      final album = pyLower(_text(pyOr([t['album'], t['album_title'], ''])));
      final text = '$genre $title $album';
      for (final kw in kws) {
        if (kw.isNotEmpty && text.contains(kw)) score += 4.0;
      }
      score += random.random() * 2.0;
      return score;
    }

    // sorted(key=…) computes each key once, in list order.
    final keyed = [for (final t in tracks) (moodScore(t), t)];
    final ranked = _stableSorted(keyed, (x, y) => y.$1.compareTo(x.$1));
    final selected = [for (final (_, t) in ranked.take(15)) t];
    random.shuffle(selected);
    final engineTracks = [for (final t in selected) toEngineTrack(t)];
    player.setQueue(engineTracks);
    _remember(engineTracks.first['path']);
    final first = selected.first;
    final title = pyStr(
      pyOr([first['title'], first['track_title'], 'Unknown']),
    );
    final artist = pyStr(
      pyOr([first['artist'], first['artist_name'], 'Unknown Artist']),
    );
    final affirmative = say('affirmative');
    return AssistantResponse(
      spoken: say('mood_steer', {
        'mood': mood,
        'title': title,
        'artist': artist,
        'affirmative': affirmative,
      }),
      displayed:
          'Curated **${pyCapitalize(mood)} Mix** (${selected.length} tracks). '
          'Playing: **$title** — $artist.',
      extras: {'track': first, 'mood': mood},
      deferredPlay: true,
    );
  }

  /// `_current_track_dict`: the database row for the playing path (minus
  /// the timbre BLOB), else the engine's strings, else the queue row.
  Future<Row?> _currentTrackDict() async {
    final path = player.currentPath;
    if (path.isNotEmpty) {
      Row? row;
      try {
        row = await library.getTrackFull(path);
      } catch (_) {}
      if (row != null) return Map.of(row)..remove('timbre');
    }
    final title = player.currentTrack;
    if (title.isNotEmpty) {
      return {
        'path': path,
        'title': title,
        'artist': player.currentArtist,
        'album': player.currentAlbum,
        'duration': pyOr([player.duration, 0.0]),
      };
    }
    final queue = player.queue;
    final idx = player.currentIndex;
    if (idx >= 0 && idx < queue.length) return queue[idx];
    return null;
  }

  Future<AssistantResponse> _handleTrackInfo() async {
    final current = await _currentTrackDict();
    if (current == null) {
      return AssistantResponse(
        spoken: 'No track is currently playing, sir.',
        displayed: 'No active track playing.',
        success: false,
      );
    }
    final title = pyStr(
      pyOr([current['track_title'], current['title'], 'Unknown Track']),
    );
    final artist = pyStr(
      pyOr([current['artist_name'], current['artist'], 'Unknown Artist']),
    );
    final album = pyStr(
      pyOr([
        current['album_title'],
        current['album'],
        'Single / Unknown Album',
      ]),
    );
    final d = pyOr([_getOr(current, 'duration', 0.0), 0.0]);
    final duration = switch (d) {
      num n => n.toDouble(),
      String s => double.tryParse(pyStrip(s)) ?? 0.0,
      _ => 0.0,
    };
    final durStr = duration > 0
        ? '${duration ~/ 60}m ${(duration % 60).toInt()}s'
        : 'Unknown length';
    return AssistantResponse(
      spoken: say('track_info', {
        'title': title,
        'artist': artist,
        'album': album,
        'duration': durStr,
      }),
      displayed:
          '**Track Info**\n• Title: **$title**\n• Artist: **$artist**\n'
          '• Album: **$album**\n• Duration: **$durStr**',
      extras: {'track': current},
    );
  }
}
