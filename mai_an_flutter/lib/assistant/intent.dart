import '../core/py_re.dart';
import '../core/py_text.dart';

/// `assistant_intent.py`: maps one utterance to a typed intent with a
/// deterministic, regex-driven matcher (no ML, no LLM). The patterns are
/// Flet's, character for character, run through [PyRe] so they match what
/// Python's `re` matches. Order matters: the first pattern that matches
/// wins, and `unknown` is what's left.

const intentPlayNow = 'play_now';
const intentQueueAdd = 'queue_add';
const intentQueueNext = 'queue_next';
const intentPlaySimilar = 'play_similar';
const intentPlayMoreBy = 'play_more_by';
const intentSkip = 'skip';
const intentPrev = 'prev';
const intentPause = 'pause';
const intentResume = 'resume';
const intentStop = 'stop';
const intentClearQueue = 'clear_queue';
const intentMute = 'mute';
const intentUnmute = 'unmute';
const intentShuffle = 'shuffle';
const intentPlayRandom = 'play_random';
const intentPlayTheUsual = 'play_the_usual';
const intentAffirmative = 'affirmative';
const intentNegative = 'negative';
const intentChoiceSelect = 'choice_select';
const intentQueueRemove = 'queue_remove';
const intentQueueMove = 'queue_move';
const intentSaveQueue = 'save_queue';
const intentMoodSteer = 'mood_steer';
const intentTrackInfo = 'track_info';
const intentNameEntity = 'name_entity';
const intentGreet = 'greet';
const intentHelp = 'help';
const intentCreator = 'creator';
const intentCoffee = 'coffee';
const intentHal = 'hal';
const intentIronMan = 'iron_man';
const intentJoke = 'joke';
const intentTimeDate = 'time_date';
const intentStatus = 'status';
const intentThanks = 'thanks';
const intentQuote = 'quote';
const intentUnknown = 'unknown';

/// `Intent`: the typed result of parsing one utterance. [extras] is
/// mutable: the runner adds resolution state to it, as Flet's does.
class Intent {
  Intent(this.name, {this.query, this.raw = '', Map<String, Object?>? extras})
    : extras = extras ?? {};

  final String name;
  String? query;
  final String raw;
  Map<String, Object?> extras;

  Intent copy() => Intent(name, query: query, raw: raw, extras: Map.of(extras));
}

PyRe _p(String pattern) => PyRe(pattern, ignoreCase: true);

/// `_build_patterns()`, in Flet's order.
final List<(String, PyRe)> _patterns = [
  // Confirmation routine (highest priority).
  (
    intentAffirmative,
    _p(
      r"^\s*(?:"
      r"yes|yeah|yep|yup|yea|yesa|yess+|yessir|yupp+|yeep|"
      r"sure|sure\s+thing|surely|suree|ok|okay|okk|okayy|alright|alrighty|"
      r"do\s+it|do\s+that|go\s+ahead|go\s+for\s+it|please\s+do|yes\s+please|yeah\s+please|"
      r"apply(?:\s+it|\s+suggestion|\s+that)?|save(?:\s+it|\s+suggestion|\s+that)?|accept(?:\s+it|\s+suggestion|\s+that)?|"
      r"confirm|confirmed|affirmative|sounds\s+good|sounds\s+great|"
      r"proceed|please\s+proceed|do\s+proceed|make\s+it\s+so|"
      r"absolutely|definitely|indeed|of\s+course|"
      r"(?:yes|yeah|sure|ok|okay|go\s+ahead)\s+(?:and\s+)?(?:do|apply|tag|save|proceed)(?:\s+(?:it|them|that|suggestion))?"
      r")\s*[.!]?\s*$",
    ),
  ),
  (
    intentNegative,
    _p(
      r"^\s*(?:"
      r"no|nope|nah|noo+|noa|nay|"
      r"no\s+thanks?|no\s+thank\s+you|not\s+now|later|cancel|cancelled|stop|"
      r"forget\s+it|negative|never\s+mind|nevermind|don't(?:\s+do\s+it|\s+apply)?|"
      r"skip(?:\s+it)?|dismiss(?:\s+it)?"
      r")\s*[.!]?\s*$",
    ),
  ),
  // Choice selection (multi-choice pending dialogue).
  (
    intentChoiceSelect,
    _p(
      r"^\s*(?:option|choice|number|#)?\s*(?P<q>1|2|3|4|5|one|two|three|four|five|first|second|third|fourth|fifth)(?:\s+one)?\s*$",
    ),
  ),
  // Naming / entity specification.
  (
    intentNameEntity,
    _p(
      r"^\s*(?:call\s+(?:the\s+)?playlist|name\s+(?:the\s+)?playlist|call\s+it|name\s+it|make\s+it|called|named|titled)\s+(?P<q>.+?)\s*$",
    ),
  ),
  // Verbless single-word commands.
  (
    intentGreet,
    _p(
      r"^\s*(?:hello|hi|hey|good\s+(?:morning|afternoon|evening)|greetings|yo)\s*(?:jarvis)?\s*[.!]?\s*$",
    ),
  ),
  (
    intentHelp,
    _p(
      r"^\s*(?:help|what can you do|commands?|what can i say|show help|info)\s*\??\s*$",
    ),
  ),
  (
    intentSkip,
    _p(r"^\s*(?:skip|next|fwd|forward|next\s+track|next\s+song)\s*$"),
  ),
  (
    intentPrev,
    _p(r"^\s*(?:previous|prev|back|last|previous\s+track|previous\s+song)\s*$"),
  ),
  (intentPause, _p(r"^\s*(?:pause|hold|wait)\s*$")),
  (intentResume, _p(r"^\s*(?:resume|continue|unpause|keep\s+going|play)\s*$")),
  (intentStop, _p(r"^\s*stop\s*(?:playing|music)?\s*$")),
  (intentClearQueue, _p(r"^\s*(?:clear|empty|wipe)\s+(?:the\s+)?queue\s*$")),
  (intentShuffle, _p(r"^\s*(?:shuffle|randomi[sz]e)(?:\s+the\s+queue)?\s*$")),
  (intentMute, _p(r"^\s*(?:mute|silence|be\s+quiet)\s*$")),
  (intentUnmute, _p(r"^\s*(?:unmute|restore\s+volume)\s*$")),
  // Conversational and easter eggs.
  (
    intentCreator,
    _p(
      r"^\s*(?:who\s+(?:made|created|built|programmed)\s+you|who\s+is\s+your\s+(?:creator|father|maker)|who\s+are\s+you)\s*\??\s*$",
    ),
  ),
  (
    intentCoffee,
    _p(
      r"^\s*(?:make|brew|get|pour)\s+(?:me\s+)?(?:a\s+)?(?:coffee|tea|cappuccino|espresso|drink)\s*[.!]?\s*$",
    ),
  ),
  (
    intentHal,
    _p(r"^\s*(?:open\s+(?:the\s+)?pod\s+bay\s+doors?|hal\s*9000)\s*[.!]?\s*$"),
  ),
  (intentIronMan, _p(r"^\s*(?:i\s+am|i'm)\s+iron\s+man\s*[.!]?\s*$")),
  (
    intentJoke,
    _p(
      r"^\s*(?:tell\s+me\s+(?:a\s+)?joke|say\s+something\s+funny|make\s+me\s+laugh|got\s+any\s+jokes?)\s*\??\s*$",
    ),
  ),
  (
    intentTimeDate,
    _p(
      r"^\s*(?:what\s*'?s?\s+(?:the\s+|today's\s+)?(?:time|date)|what\s+time\s+is\s+it|what\s+is\s+the\s+(?:time|date)|what\s+day\s+is\s+it|tell\s+me\s+the\s+time|current\s+time)\s*\??\s*$",
    ),
  ),
  (
    intentStatus,
    _p(
      r"^\s*(?:system\s+status|status\s+report|run\s+diagnostics?|how\s+are\s+you(?: doing)?|how's\s+it\s+going|how\s+are\s+things)\s*\??\s*$",
    ),
  ),
  (
    intentThanks,
    _p(
      r"^\s*(?:thank\s+you|thanks(?:\s+jarvis)?|good\s+job|great\s+job|cheers|awesome|nice\s+one)\s*[.!]?\s*$",
    ),
  ),
  (
    intentQuote,
    _p(
      r"^\s*(?:give\s+me\s+a\s+quote|quote\s+(?:me|something)|say\s+something\s+(?:wise|inspirational|philosophical))\s*\??\s*$",
    ),
  ),
  // Song metadata and trivia.
  (
    intentTrackInfo,
    _p(
      r"^\s*(?:tell\s+me\s+(?:about|more\s+about)|what\s+is|info\s+on|trivia\s+on)\s+(?:this|the|current)\s+(?:track|song|music|album)\s*\??\s*$",
    ),
  ),
  (intentTrackInfo, _p(r"^\s*(?:track|song)\s+info(?:rmation)?\s*\??\s*$")),
  // Similarity, mood, curation.
  (
    intentMoodSteer,
    _p(
      r"^\s*(?:play|make|set)\s+(?:something\s+|the\s+queue\s+|a\s+|some\s+)?"
      r"(?P<q>chill|relaxing|calm|quiet|energetic|upbeat|fast|slow|intense|focus|workout|melancholic|nostalgic|rainy|dark|heavy|euphoric|mellow|party|hype|ambient)\s*"
      r"(?:music|tracks?|songs?|vibes?|mix|for\s+reading|for\s+studying|for\s+running|for\s+working)?\s*$",
    ),
  ),
  (
    intentPlaySimilar,
    _p(
      r"^\s*(?P<verb>play|start|put\s+on|add|queue|enqueue|put)?\s*"
      r"(?:a\s+|some\s+|something\s+|stuff\s+|tracks?\s+|songs?\s+|music\s+|tunes?\s+)?"
      r"(?:more\s+)?similar"
      r"(?:\s+(?:songs?|tracks?|music|tunes?|stuff))?"
      r"(?:\s+(?:like|similar\s+to|to)\s+(?:this|that|current))?\s*$",
    ),
  ),
  (
    intentPlaySimilar,
    _p(
      r"^\s*(?P<verb>play|start|put\s+on|add|queue|enqueue|put)?\s*"
      r"(?:more\s+)?like\s+(?:this|that|current)\s*$",
    ),
  ),
  (
    intentPlayMoreBy,
    _p(r"^\s*(?:play\s+)?more\s+(?:by|from)\s+(?:this|that|the)\s+artist\s*$"),
  ),
  (
    intentPlayRandom,
    _p(
      r"^\s*(?:play|start|put\s+on|add|queue|enqueue|put)?\s*"
      r"(?:me\s+)?"
      r"(?:a\s+|some\s+|any\s+)?\s*"
      r"(?:random\s+)?\s*"
      r"(?:song|songs|track|tracks|music|tune|tunes|stuff|anything|something)\s*$",
    ),
  ),
  (
    intentPlayRandom,
    _p(
      r"^\s*(?:shuffle\s+play|surprise\s+me|sing(?:\s+a\s+song|\s+something)?)\s*\??\s*$",
    ),
  ),
  // "play something else" / "play another": a fresh shuffle pick.
  (
    intentPlayRandom,
    _p(
      r"^\s*(?:play|start|put\s+on|give\s+me|queue)?\s*(?:me\s+)?"
      r"(?:"
      r"(?:something|anything)\s+(?:else|different|new)"
      r"|another(?:\s+(?:one|song|track|tune))?"
      r")\s*$",
    ),
  ),
  (
    intentPlayTheUsual,
    _p(r"^\s*(?:play|queue\s+up|give\s+me)\s+(?:the|my)\s+usual\s*$"),
  ),
  // Queue and walk operations with a query.
  (
    intentSaveQueue,
    _p(
      r"^\s*(?:save|create|make)\s+(?:the\s+|this\s+|current\s+)?(?:queue|walk)\s+as\s+(?:a\s+)?(?:playlist\s+)?(?P<q>.+?)\s*$",
    ),
  ),
  (
    intentSaveQueue,
    _p(
      r"^\s*(?:save|create|make)\s+(?:a\s+)?playlist\s+(?:called|named|from\s+(?:the\s+)?(?:queue|walk))?\s+(?P<q>.+?)\s*$",
    ),
  ),
  (
    intentSaveQueue,
    _p(
      r"^\s*save\s+(?:the\s+|this\s+|current\s+)?walk(?:\s+to\s+(?:a\s+)?playlist)?(?:\s+(?P<q>.+?))?\s*$",
    ),
  ),
  (
    intentQueueMove,
    _p(
      r"^\s*(?:move|shift|put)\s+(?P<q>.+?)\s+(?:to\s+(?:the\s+)?top|next|first)\s*$",
    ),
  ),
  (
    intentQueueRemove,
    _p(
      r"^\s*(?:remove|delete|drop|take\s+off)\s+(?P<q>.+?)(?:\s+(?:from|in)\s+(?:the\s+)?queue)?\s*$",
    ),
  ),
  (intentQueueNext, _p(r"^\s*(?:play\s+)?(?P<q>.+?)\s+next\s*$")),
  (
    intentQueueAdd,
    _p(
      r"^\s*(?:add|queue|enqueue|put)\s+(?P<q>.+?)(?:\s+(?:to|in)\s+(?:the\s+)?queue)?\s*$",
    ),
  ),
  // Play X (catch-all for non-trivial 'play …' phrasings).
  (intentPlayNow, _p(r"^\s*(?:play|start|put\s+on)\s+(?P<q>.+?)\s*$")),
];

/// The named groups each pattern can carry, in pattern order (Python's
/// `groupdict()` order). Only these two exist.
const _groupNames = ['verb', 'q'];

/// Politeness and filler stripped from the end ("play stairway please").
final _trailingFiller = _p(
  r"\s+(?:please|for\s+me|on\s+spotify|on\s+qobuz|thanks?|immediately|right\s+now)\s*$",
);

/// Leading filler and wake words ("hey jarvis, play X" → "play X").
final _leadingFiller = _p(
  r"^\s*(?:"
  r"hey|ok|okay|yo|hi|hello|assistant|jarvis|"
  r"um|uh|err|ah|eh|hmm|so|well|like|just|"
  r"please|can\s+you|could\s+you|would\s+you(?:\s+mind)?|"
  r"let's|let\s+us|i\s+(?:want|would\s+like)\s+to"
  r")[,\s]+",
);

final _spaces = PyRe(r"\s+");
final _trailingPunct = PyRe(r"[.!?]+\s*$");

String _normalise(String raw) {
  var s = pyStrip(raw);
  String? prev;
  while (prev != s) {
    prev = s;
    s = _leadingFiller.sub(s, '');
    s = _trailingFiller.sub(s, '');
    s = pyStrip(s);
  }
  s = pyStrip(_spaces.sub(s, ' '));
  return pyStrip(_trailingPunct.sub(s, ''));
}

const _leadFillers = [
  'some', 'any', 'a', 'an', 'the', //
  'me', 'for',
  'song', 'songs', 'track', 'tracks', 'tune', 'tunes', 'album', 'albums',
  'cue', 'queue',
  'by', 'from',
];

final _multiFiller = _p(
  r"^(?:(?:something|anything|stuff|music)\s+(?:by|from))\s+",
);
final _leadFiller = _p('^(?:${_leadFillers.join('|')})\\s+');
final _trailingNoise = _p(r"\s+(?:songs?|tracks?|tunes?|albums?|music)\s*$");
final _trailingQueryPunct = PyRe(r"[.!?,\s]+$");

/// Python's `s.strip("\"'")`.
String _stripQuotes(String s) {
  var a = 0, b = s.length;
  bool q(int c) => c == 0x22 || c == 0x27;
  while (a < b && q(s.codeUnitAt(a))) {
    a++;
  }
  while (b > a && q(s.codeUnitAt(b - 1))) {
    b--;
  }
  return s.substring(a, b);
}

/// `_clean_query`: peels filler off the query slot until it stops
/// changing ("me a beatles song" → "beatles").
String cleanQuery(String q) {
  q = pyStrip(_stripQuotes(pyStrip(q)));
  String? prev;
  while (prev != q) {
    prev = q;
    q = _multiFiller.sub(q, '');
    q = _leadFiller.sub(q, '');
    q = _trailingNoise.sub(q, '');
    q = pyStrip(_trailingQueryPunct.sub(q, ''));
    q = pyStrip(q);
  }
  return q;
}

const _greetLead =
    r"hello|hi|hey|good\s+(?:morning|afternoon|evening)|greetings|yo|jarvis";
final _greetOnly = _p(
  '^\\s*(?:$_greetLead)\\s*(?:$_greetLead)?\\s*[.!?\\s]*\$',
);

/// `parse(text)`. Returns `unknown` when nothing matches; the runner then
/// hands the turn to the agent, or falls back to a library search.
Intent parse(String? text) {
  final raw = text ?? '';
  // Greetings and the bare wake word, before _normalise strips them away.
  if (_greetOnly.match(raw) != null) return Intent(intentGreet, raw: raw);

  final normalised = _normalise(raw);
  if (normalised.isEmpty) return Intent(intentUnknown, raw: raw);

  for (final (name, pattern) in _patterns) {
    final m = pattern.match(normalised);
    if (m == null) continue;
    final groups = m.groupdict([
      for (final g in _groupNames)
        if (pattern.regExp.pattern.contains('(?<$g>')) g,
    ]);
    final extras = <String, Object?>{
      for (final e in groups.entries)
        if (e.key != 'q' && e.value != null) e.key: cleanQuery(e.value!),
    };
    final q = groups['q'];
    return Intent(
      name,
      query: (q == null || q.isEmpty) ? null : cleanQuery(q),
      raw: raw,
      extras: extras,
    );
  }
  return Intent(intentUnknown, raw: raw);
}
