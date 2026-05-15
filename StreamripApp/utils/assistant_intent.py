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
from dataclasses import dataclass, field
from typing import Optional


# Intent constants — keep aligned with assistant_runner.dispatch().
INTENT_PLAY_NOW       = "play_now"        # play X immediately
INTENT_QUEUE_ADD      = "queue_add"       # add X to queue
INTENT_QUEUE_NEXT     = "queue_next"      # play X next (insert after current)
INTENT_PLAY_SIMILAR   = "play_similar"    # play more like current track
INTENT_PLAY_MORE_BY   = "play_more_by"    # more by current artist
INTENT_DOWNLOAD       = "download"        # download X
INTENT_SKIP           = "skip"            # next track
INTENT_PREV           = "prev"            # previous track
INTENT_PAUSE          = "pause"
INTENT_RESUME         = "resume"
INTENT_STOP           = "stop"
INTENT_CLEAR_QUEUE    = "clear_queue"
INTENT_MUTE           = "mute"
INTENT_UNMUTE         = "unmute"
INTENT_SHUFFLE        = "shuffle"
INTENT_NOW_PLAYING    = "now_playing"     # what's playing
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

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # ── Verbless single-word commands first ─────────────────────────────────
    (INTENT_HELP,         re.compile(r"^\s*(?:help|what can you do|commands?)\s*\??\s*$", re.I)),
    (INTENT_NOW_PLAYING,  re.compile(r"^\s*(?:what(?:'s| is)\s+(?:this|playing|on)|now\s+playing|current\s+(?:song|track))\s*\??\s*$", re.I)),
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
    (INTENT_PLAY_SIMILAR, re.compile(r"^\s*(?:play\s+)?(?:something|stuff|tracks?|songs?)\s+(?:like|similar\s+to)\s+(?:this|that|current)\s*$", re.I)),
    (INTENT_PLAY_SIMILAR, re.compile(r"^\s*more\s+(?:like\s+)?(?:this|that)\s*$", re.I)),
    (INTENT_PLAY_MORE_BY, re.compile(r"^\s*(?:play\s+)?more\s+(?:by|from)\s+(?:this|that|the)\s+artist\s*$", re.I)),

    # ── Queue ops with query ────────────────────────────────────────────────
    (INTENT_QUEUE_NEXT, re.compile(
        r"^\s*(?:play\s+)?(?P<q>.+?)\s+next\s*$", re.I
    )),
    (INTENT_QUEUE_ADD, re.compile(
        r"^\s*(?:add|queue|enqueue)\s+(?P<q>.+?)(?:\s+(?:to\s+(?:the\s+)?queue|to\s+queue))?\s*$",
        re.I,
    )),

    # ── Download ────────────────────────────────────────────────────────────
    (INTENT_DOWNLOAD, re.compile(
        r"^\s*(?:download|get|grab|fetch|save)\s+(?P<q>.+?)\s*$", re.I
    )),

    # ── Play X (catch-all for non-trivial 'play ...' phrasings) ────────────
    (INTENT_PLAY_NOW, re.compile(
        r"^\s*(?:play|start|put\s+on)\s+(?P<q>.+?)\s*$", re.I
    )),
]


# Common politeness / filler trailing tokens. Stripped post-match so
# "play stairway please" doesn't become a literal search for that string.
_TRAILING_FILLER = re.compile(
    r"\s+(?:please|now|for\s+me|on\s+spotify|on\s+qobuz|thanks?)\s*$",
    re.I,
)
# Leading filler ("hey assistant, play X" → "play X").
_LEADING_FILLER = re.compile(
    r"^\s*(?:hey|ok|okay|please|yo|um|uh|hi)[,\s]+(?:assistant[,\s]+)?",
    re.I,
)


def _normalise(raw: str) -> str:
    s = raw.strip()
    s = _LEADING_FILLER.sub("", s)
    s = _TRAILING_FILLER.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing punctuation that doesn't add meaning.
    s = re.sub(r"[.!?]+\s*$", "", s).strip()
    return s


def _clean_query(q: str) -> str:
    """Strip leftover filler from the query slot."""
    q = q.strip().strip("\"'").strip()
    # 'play the song stairway' → 'stairway'
    q = re.sub(r"^(?:the\s+)?(?:song|track|tune|album)\s+", "", q, flags=re.I)
    return q.strip()


def parse(text: str) -> Intent:
    """Parse one user utterance into a typed Intent.

    Returns Intent(name=INTENT_UNKNOWN, raw=...) when nothing matches; the
    runner uses that as the cue to either ask for clarification or to
    fall back to a free-text library search."""
    raw = text or ""
    normalised = _normalise(raw)
    if not normalised:
        return Intent(name=INTENT_UNKNOWN, raw=raw)

    for name, pattern in _PATTERNS:
        m = pattern.match(normalised)
        if not m:
            continue
        groups = m.groupdict()
        q = groups.get("q") if groups else None
        return Intent(
            name=name,
            query=_clean_query(q) if q else None,
            raw=raw,
        )

    return Intent(name=INTENT_UNKNOWN, raw=raw)


__all__ = [
    "Intent",
    "parse",
    # Intent constants exported so the runner can match against them by name.
    "INTENT_PLAY_NOW",
    "INTENT_QUEUE_ADD",
    "INTENT_QUEUE_NEXT",
    "INTENT_PLAY_SIMILAR",
    "INTENT_PLAY_MORE_BY",
    "INTENT_DOWNLOAD",
    "INTENT_SKIP",
    "INTENT_PREV",
    "INTENT_PAUSE",
    "INTENT_RESUME",
    "INTENT_STOP",
    "INTENT_CLEAR_QUEUE",
    "INTENT_MUTE",
    "INTENT_UNMUTE",
    "INTENT_SHUFFLE",
    "INTENT_NOW_PLAYING",
    "INTENT_HELP",
    "INTENT_UNKNOWN",
]
