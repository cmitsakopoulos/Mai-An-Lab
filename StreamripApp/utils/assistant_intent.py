"""
Assistant intent parser.

Maps free-form user text to a typed intent the runner can dispatch. This is a
deterministic, regex-driven matcher — no ML, no LLM. The grammar is small but
covers the queue + library + download surface the assistant supports today.

Design notes:

  • Match order matters. The catch-all `unknown` fallback is intentionally at
    the bottom; everything above it is checked top-down so more-specific
    phrasings win (e.g. "add X to queue" must match queue_add before being
    swallowed by the generic "play X" branch).

  • All patterns use re.IGNORECASE and anchor with `\b` boundaries where
    possible so users don't have to capitalise correctly. Whitespace runs are
    treated as one whitespace token.

  • Query extraction is *trailing* — we strip the leading verb, the
    middle filler ("to the queue", "for me", "please"), and treat whatever's
    left as the search query. This is brittle but predictable; users who type
    "play stairway" get "stairway" as the query.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# Intent constants — keep aligned with assistant_runner.dispatch().
INTENT_PLAY_NOW       = "play_now"        # play X immediately
INTENT_QUEUE_ADD      = "queue_add"       # add X to queue
INTENT_QUEUE_NEXT     = "queue_next"      # play X next (insert after current)
INTENT_PLAY_SIMILAR   = "play_similar"    # play more like current track
INTENT_PLAY_MORE_BY   = "play_more_by"    # more by current artist
INTENT_SKIP           = "skip"            # next track
INTENT_PREV           = "prev"            # previous track
INTENT_PAUSE          = "pause"
INTENT_RESUME         = "resume"
INTENT_STOP           = "stop"
INTENT_CLEAR_QUEUE    = "clear_queue"
INTENT_MUTE           = "mute"
INTENT_UNMUTE         = "unmute"
INTENT_SHUFFLE        = "shuffle"
INTENT_PLAY_RANDOM    = "play_random"     # play a random song and shuffle
INTENT_PLAY_THE_USUAL = "play_the_usual"    # play a track from most played
INTENT_RESCAN_DSP     = "rescan_dsp"      # run analyser for missing tracks
INTENT_AFFIRMATIVE    = "affirmative"     # yes / yeah / do it (confirmation)
INTENT_NEGATIVE       = "negative"        # no / later / not now (cancel pending)
INTENT_NAME_ENTITY    = "name_entity"     # call it X / name it X
INTENT_GREET          = "greet"
INTENT_HELP           = "help"
INTENT_UNKNOWN        = "unknown"


@dataclass
class Intent:
    """Typed result of parsing one utterance."""
    name: str
    query: Optional[str] = None
    raw: str = ""
    extras: dict = field(default_factory=dict)


# ── Patterns ─────────────────────────────────────────────────────────────────
#
# Compiled once at import time. Each tuple is (intent_name, pattern). The
# pattern's named group `q` is taken as the query if present.
#
# Order matters: more specific patterns first.

def _build_patterns() -> list[tuple[str, re.Pattern]]:
    return [
        # ── Confirmation routine (highest priority) ─────────────────────────────
        # Match these BEFORE play/queue so a bare "yes" or "no" during a
        # pending-confirmation turn doesn't get misread as a search query.
        (INTENT_AFFIRMATIVE,  re.compile(
            r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|do\s+it|go\s+ahead|please\s+do|"
            r"confirm|affirmative|sounds\s+good|alright|proceed|please\s+proceed|do\s+proceed|"
            r"make\s+it\s+so|absolutely|definitely|indeed|of\s+course)\s*[.!]?\s*$", re.I)),
        (INTENT_NEGATIVE,     re.compile(
            r"^\s*(?:no|nope|nah|not\s+now|later|cancel|stop|forget\s+it|"
            r"negative|never\s+mind|nevermind)\s*[.!]?\s*$", re.I)),

        # ── Naming / Entity Specification ───────────────────────────────────────
        (INTENT_NAME_ENTITY, re.compile(
            r"^\s*(?:call\s+(?:the\s+)?playlist|name\s+(?:the\s+)?playlist|call\s+it|name\s+it|make\s+it|called|named|titled)\s+(?P<q>.+?)\s*$",
            re.I
        )),

        # ── Manual graph maintenance ────────────────────────────────────────────
        # Verb-only: "rescan", "reindex", "reanalyse".
        (INTENT_RESCAN_DSP,   re.compile(
            r"^\s*(?:rescan|re-?scan|reindex|re-?index|re-?analy[sz]e)\s*$",
            re.I)),
        # Verb + object: covers most natural phrasings, including modifiers
        # like "new" ("analyse new tracks") and "the/my" ("scan the library").
        (INTENT_RESCAN_DSP,   re.compile(
            r"^\s*(?:rescan|re-?scan|reindex|re-?index|analy[sz]e|"
            r"rebuild|refresh|update|scan)\s+"
            r"(?:(?:my|the|new|for\s+new|all)\s+)*"
            r"(?:library|graph|music|tracks?|songs?|dsp|features?)\s*$",
            re.I)),

        # ── Verbless single-word commands first ─────────────────────────────────
        (INTENT_GREET,        re.compile(
            r"^\s*(?:hello|hi|hey|good\s+(?:morning|afternoon|evening)|greetings|yo)\s*(?:jarvis)?\s*[.!]?\s*$",
            re.I
        )),
        (INTENT_HELP,         re.compile(r"^\s*(?:help|what can you do|commands?|what can i say|show help|info)\s*\??\s*$", re.I)),
        (INTENT_SKIP,         re.compile(r"^\s*(?:skip|next|fwd|forward|next\s+track|next\s+song)\s*$", re.I)),
        (INTENT_PREV,         re.compile(r"^\s*(?:previous|prev|back|last|previous\s+track|previous\s+song)\s*$", re.I)),
        (INTENT_PAUSE,        re.compile(r"^\s*(?:pause|hold|wait)\s*$", re.I)),
        (INTENT_RESUME,       re.compile(r"^\s*(?:resume|continue|unpause|keep\s+going|play)\s*$", re.I)),
        (INTENT_STOP,         re.compile(r"^\s*stop\s*(?:playing|music)?\s*$", re.I)),
        (INTENT_CLEAR_QUEUE,  re.compile(r"^\s*(?:clear|empty|wipe)\s+(?:the\s+)?queue\s*$", re.I)),
        (INTENT_SHUFFLE,      re.compile(r"^\s*(?:shuffle|randomi[sz]e)(?:\s+the\s+queue)?\s*$", re.I)),
        (INTENT_MUTE,         re.compile(r"^\s*(?:mute|silence|be\s+quiet)\s*$", re.I)),
        (INTENT_UNMUTE,       re.compile(r"^\s*(?:unmute|restore\s+volume)\s*$", re.I)),

        # ── Similarity / artist navigation ──────────────────────────────────────
        (INTENT_PLAY_SIMILAR, re.compile(
            r"^\s*(?P<verb>play|start|put\s+on|add|queue|enqueue|put)?\s*"
            r"(?:a\s+|some\s+|something\s+|stuff\s+|tracks?\s+|songs?\s+|music\s+|tunes?\s+)?"
            r"(?:more\s+)?similar"
            r"(?:\s+(?:songs?|tracks?|music|tunes?|stuff))?"
            r"(?:\s+(?:like|similar\s+to|to)\s+(?:this|that|current))?\s*$",
            re.I
        )),
        (INTENT_PLAY_SIMILAR, re.compile(
            r"^\s*(?P<verb>play|start|put\s+on|add|queue|enqueue|put)?\s*"
            r"(?:more\s+)?like\s+(?:this|that|current)\s*$",
            re.I
        )),
        (INTENT_PLAY_MORE_BY, re.compile(r"^\s*(?:play\s+)?more\s+(?:by|from)\s+(?:this|that|the)\s+artist\s*$", re.I)),
        (INTENT_PLAY_RANDOM,  re.compile(
            r"^\s*(?:play|start|put\s+on|add|queue|enqueue|put)?\s*"
            r"(?:me\s+)?"
            r"(?:a\s+|some\s+|any\s+)?\s*"
            r"(?:random\s+)?\s*"
            r"(?:song|songs|track|tracks|music|tune|tunes|stuff|anything|something)\s*$",
            re.I
        )),
        (INTENT_PLAY_RANDOM,  re.compile(r"^\s*(?:shuffle\s+play|surprise\s+me)\s*$", re.I)),
        # "play something else" / "play another" — fresh shuffle pick. Without
        # this the parser fell through to play_now and searched the library
        # for a literal track called "something else".
        (INTENT_PLAY_RANDOM,  re.compile(
            r"^\s*(?:play|start|put\s+on|give\s+me|queue)?\s*(?:me\s+)?"
            r"(?:"
            r"(?:something|anything)\s+(?:else|different|new)"
            r"|another(?:\s+(?:one|song|track|tune))?"
            r")\s*$",
            re.I,
        )),
        (INTENT_PLAY_THE_USUAL, re.compile(
            r"^\s*(?:play|queue\s+up|give\s+me)\s+(?:the|my)\s+usual\s*$",
            re.I,
        )),



        # ── Queue ops with query ────────────────────────────────────────────────
        (INTENT_QUEUE_NEXT, re.compile(
            r"^\s*(?:play\s+)?(?P<q>.+?)\s+next\s*$", re.I
        )),
        (INTENT_QUEUE_ADD, re.compile(
            r"^\s*(?:add|queue|enqueue|put)\s+(?P<q>.+?)(?:\s+(?:to|in)\s+(?:the\s+)?queue)?\s*$",
            re.I,
        )),

        # ── Play X (catch-all for non-trivial 'play ...' phrasings) ────────────
        (INTENT_PLAY_NOW, re.compile(
            r"^\s*(?:play|start|put\s+on)\s+(?P<q>.+?)\s*$", re.I
        )),
    ]

_PATTERNS = _build_patterns()


# Common politeness / filler trailing tokens. Stripped post-match so
# "play stairway please" doesn't become a literal search for that string.
# NOTE: "now" was here but collided with "not now" (the negative confirmation
# phrasing), turning it into a bare "not" that no pattern recognises.
# "play radiohead now" still parses fine without the strip — the trailing
# "now" gets captured into the query slot and the LIKE search ignores it.
_TRAILING_FILLER = re.compile(
    r"\s+(?:please|for\s+me|on\s+spotify|on\s+qobuz|thanks?|immediately|right\s+now)\s*$",
    re.I,
)
# Leading filler ("hey assistant, play X" → "play X").
# Includes wake words like "jarvis" and common conversational starters/hesitations.
_LEADING_FILLER = re.compile(
    r"^\s*(?:"
    r"hey|ok|okay|yo|hi|hello|assistant|jarvis|"
    r"um|uh|err|ah|eh|hmm|so|well|like|just|"
    r"please|can\s+you|could\s+you|would\s+you(?:\s+mind)?|"
    r"let's|let\s+us|i\s+(?:want|would\s+like)\s+to"
    r")[,\s]+",
    re.I,
)


def _normalise(raw: str) -> str:
    s = raw.strip()
    # Fixed-point loop to peel off multiple stacked leading/trailing fillers
    # e.g., "Yo, um, Jarvis, play X please" -> "play X"
    prev = None
    while prev != s:
        prev = s
        s = _LEADING_FILLER.sub("", s)
        s = _TRAILING_FILLER.sub("", s)
        s = s.strip()
        
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing punctuation that doesn't add meaning.
    s = re.sub(r"[.!?]+\s*$", "", s).strip()
    return s


def _clean_query(q: str) -> str:
    """Strip leftover filler from the query slot so the library search hits
    the actual entity name. Casual phrasings drop "play some radiohead",
    "play me a beatles song", "play any pink floyd" — without this pass
    the SQL LIKE search treats "some radiohead" as a literal substring and
    returns zero hits even though the artist is right there.

    Cleaning runs as a fixed-point loop: each pass peels one filler layer,
    and we re-run until the string stops shrinking. This handles chained
    fillers like "me a beatles" (→ "a beatles" → "beatles") without
    needing to enumerate every n-way combination in the regex."""
    q = q.strip().strip("\"'").strip()

    # Single-token leading words that act as fillers when followed by the
    # entity. Ordered for clarity, not priority — the loop re-runs anyway.
    _LEAD_FILLERS = (
        # quantifiers / articles
        "some", "any", "a", "an", "the",
        # politeness / pronouns
        "me", "for",
        # "song/track/album/tune/tunes" noise word
        "song", "songs", "track", "tracks", "tune", "tunes", "album", "albums",
        # homophones / synonyms for queue
        "cue", "queue",
        # "by" / "from" — bare leading prepositions
        "by", "from",
    )
    lead_alt = "|".join(re.escape(w) for w in _LEAD_FILLERS)
    _MULTI_FILLER_PHRASES = (
        # "something by", "anything by", "stuff from" etc. as a single token
        r"(?:something|anything|stuff|music)\s+(?:by|from)",
    )

    prev = None
    while prev != q:
        prev = q
        # Strip multi-word filler phrases first (they include a single
        # leading word that would otherwise be matched in isolation).
        for phrase in _MULTI_FILLER_PHRASES:
            q = re.sub(rf"^(?:{phrase})\s+", "", q, flags=re.I)
        # Then peel a single leading filler word per iteration.
        q = re.sub(rf"^(?:{lead_alt})\s+", "", q, flags=re.I)
        # Trailing noise words: "beatles song" → "beatles".
        q = re.sub(
            r"\s+(?:songs?|tracks?|tunes?|albums?|music)\s*$",
            "", q, flags=re.I,
        )
        # Strip trailing punctuation inside the query too (e.g. trailing commas, periods)
        q = re.sub(r"[.!?,\s]+$", "", q).strip()
        q = q.strip()

    return q


def parse(text: str) -> Intent:
    """Parse one user utterance into a typed Intent.

    Returns Intent(name=INTENT_UNKNOWN, raw=...) when nothing matches; the
    runner uses that as the cue to either ask for clarification or to
    fall back to a free-text library search."""
    raw = text or ""
    
    # Direct raw match for greetings/wake-word-only prompts to prevent _normalise from tearing them apart
    _GREET_LEAD = r"hello|hi|hey|good\s+(?:morning|afternoon|evening)|greetings|yo|jarvis"
    if re.match(rf"^\s*(?:{_GREET_LEAD})\s*(?:{_GREET_LEAD})?\s*[.!?\s]*$", raw, re.I):
        return Intent(name=INTENT_GREET, raw=raw)

    normalised = _normalise(raw)
    if not normalised:
        return Intent(name=INTENT_UNKNOWN, raw=raw)

    for name, pattern in _PATTERNS:
        m = pattern.match(normalised)
        if not m:
            continue
        groups = m.groupdict()
        q = groups.get("q") if groups else None
        
        # Clean extra parsed capture groups (like 'playlist' or 'track')
        extras = {}
        if groups:
            for k, v in groups.items():
                if k != "q" and v is not None:
                    extras[k] = _clean_query(v)
                    
        query_val = _clean_query(q) if q else None

        return Intent(
            name=name,
            query=query_val,
            raw=raw,
            extras=extras,
        )

    # --- SEMANTIC FALLBACK GATEWAY ---
    # If the syntactic regex loop misses, we fall back to our local BGE Vector Space Model!
    # Bypassing semantic fallback for purely numeric parameters or very short utterances
    # to prevent structural misclassification of dialog slot-filling parameters (like '5').
    if normalised.isdigit() or len(normalised) < 3:
        return Intent(name=INTENT_UNKNOWN, raw=raw)

    import time
    start_time = time.perf_counter()
    try:
        classifier = get_semantic_classifier()
        match = classifier.classify(normalised, threshold=0.50)
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        
        # Calculate simulated Android bounds
        # Fast Android (2x faster due to a modern octa-core mobile CPU vs older PC CPU)
        # Slow Android (2x slower due to standard mobile power-throttling/emulation overhead)
        android_fast_ms = duration_ms / 2.0
        android_slow_ms = duration_ms * 2.0
        
        if match:
            intent_name, anchor_phrase, score = match
            extracted_query = classifier.extract_slots(raw, intent_name)
            
            return Intent(
                name=intent_name,
                query=extracted_query,
                raw=raw,
                extras={
                    "semantic": True,
                    "score": score,
                    "anchor": anchor_phrase,
                    "compute_time_windows_ms": round(duration_ms, 2),
                    "simulated_android_fast_ms": round(android_fast_ms, 2),
                    "simulated_android_slow_ms": round(android_slow_ms, 2),
                }
            )
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"Semantic fallback failed: {e}", exc_info=True)

    return Intent(name=INTENT_UNKNOWN, raw=raw)


_SEMANTIC_CLASSIFIER = None

def get_semantic_classifier():
    """Lazy-loaded module-level singleton for the semantic classifier."""
    global _SEMANTIC_CLASSIFIER
    if _SEMANTIC_CLASSIFIER is None:
        from utils.semantic_intent import SemanticIntentClassifier
        _SEMANTIC_CLASSIFIER = SemanticIntentClassifier()
    return _SEMANTIC_CLASSIFIER


__all__ = [
    "Intent",
    "parse",
    # Intent constants exported so the runner can match against them by name.
    "INTENT_PLAY_NOW",
    "INTENT_QUEUE_ADD",
    "INTENT_QUEUE_NEXT",
    "INTENT_PLAY_SIMILAR",
    "INTENT_PLAY_MORE_BY",
    "INTENT_SKIP",
    "INTENT_PREV",
    "INTENT_PAUSE",
    "INTENT_RESUME",
    "INTENT_STOP",
    "INTENT_CLEAR_QUEUE",
    "INTENT_MUTE",
    "INTENT_UNMUTE",
    "INTENT_SHUFFLE",
    "INTENT_PLAY_RANDOM",
    "INTENT_PLAY_THE_USUAL",
    "INTENT_RESCAN_DSP",
    "INTENT_AFFIRMATIVE",
    "INTENT_NEGATIVE",
    "INTENT_NAME_ENTITY",
    "INTENT_GREET",
    "INTENT_HELP",
    "INTENT_UNKNOWN",
]
