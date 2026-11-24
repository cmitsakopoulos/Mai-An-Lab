#!/usr/bin/env python3
"""Golden values for phase 5 stage a (PORT_MAP rule 3): what the Flet app's
`assistant_intent.parse`, `llm_tools.strip_markdown` / `_slim_track`,
`web_research._fold` / `names_mentioned` and `ChatMemoryManager` produce,
and how Python's `re` matches the constructs lib/core/py_re.dart rewrites.

    test/goldens/assistant_text.json   (committed; no personal data)

The intent corpus is every short string literal in Flet's assistant tests
(so each phrase they check is here), a grid of verbs, fillers and queries,
and edge cases for the regex rewrite (Greek, accents, Turkish dotted and
dotless i, long s, Kelvin sign, NBSP, U+FEFF, U+001C, U+0085, \\r, a
trailing \\n, emoji).

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_assistant_goldens.py
"""
from __future__ import annotations

import ast
import glob
import json
import logging
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
FLET = os.path.join(REPO, "StreamripApp")
sys.path.insert(0, FLET)

from utils import assistant_intent as ai  # noqa: E402
from utils import chat_memory  # noqa: E402
from utils import llm_tools  # noqa: E402
from utils import web_research  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "assistant_text.json")


def _test_literals() -> list[str]:
    files = []
    for pat in ("test_assistant_*.py", "test_jarvis_*.py", "test_chat_memory.py",
                "test_llm_*.py"):
        files += sorted(glob.glob(os.path.join(FLET, "tests", pat)))
    out: list[str] = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if 0 < len(s) <= 80 and "\n" not in s and s not in out:
                    out.append(s)
    return out


VERBS = ["play", "start", "put on", "add", "queue", "enqueue", "put", "remove",
         "move", "save", "make", "give me", "shuffle", "skip", ""]
FILLERS = ["", "hey jarvis, ", "um, ", "Yo, um, Jarvis, ", "could you ", "I want to ",
           "please ", "let's "]
TAILS = ["", " please", " for me", " now", " right now", " next", " to the queue",
         " to top", " thanks", "!", "?", "...", " on qobuz"]
QUERIES = ["radiohead", "some radiohead", "me a beatles song", "any pink floyd",
           "something by björk", "the album OK Computer", "Αγάπη μου", "track 2",
           "similar", "more like this", "something chill", "the usual", "something else",
           "another one", "random music", "it", "that song", "them", "\"Echoes\"",
           "queue as playlist Night Drive", "walk", "", "song", "tracks"]

EDGE = [
    "", " ", "\t", "PLAY RADIOHEAD", "Play Radiohead", "play İstanbul", "PLAY ıstanbul",
    "plaY", "pLAY ſomething chill", "ſkip", "next\n", "play x\n", "play x\r",
    "play radiohead", "play﻿radiohead", "play\u001cradiohead",
    "play\u0085radiohead", "play radiohead ", "play 🎵 music 🎶",
    "﻿play radiohead", "play radiohead﻿", "  hello  jarvis  ", "hey hey",
    "jarvis", "jarvis jarvis", "hi.", "yo!", "good  morning jarvis", "HELLO?!",
    "ok", "okay.", "yesss", "Yes please", "no thanks", "not now", "don't apply",
    "#3", "number two", "Option 5", "fifth one", "6", "call it Road Trip",
    "named Ελληνικά", "what's the time", "whats today's date", "how's it going?",
    "i'm iron man", "I am IRON MAN!", "Kelvin play Korn", "play Korn",
    "play something rainy for studying", "set the queue chill vibes",
    "play some similar tracks like this", "add similar", "queue more like current",
    "play more from the artist", "surprise me?", "sing a song", "give me another tune",
    "queue up my usual", "save the walk to a playlist Late", "save walk",
    "make a playlist called Gym", "create playlist from the queue Mix",
    "move Echoes first", "take off track 3 in queue", "drop the last song",
    "play stairway next", "put Time next", "add Money", "enqueue Us and Them to queue",
    "start Wish You Were Here.", "put on Breathe!!", "play play", "play next",
    "play, please", "please play radiohead please", "hey, um, so, play x thanks",
    "trivia on current album", "song information?", "Track Info",
    "clear the queue", "wipe queue", "randomise the queue", "randomize",
    "be quiet", "restore volume", "keep going", "unpause", "status report",
    "run diagnostic", "quote something", "say something philosophical",
    "got any jokes", "make me laugh", "pour me a cappuccino!", "hal9000",
    "open the pod bay door.", "who is your maker?", "who are you",
    "what can i say", "commands", "show help?", "Στείλε μου κάτι", "παίξε Αγάπη",
    "play 'quoted' \"names\"", "play song", "play the song", "play some tracks",
    "play me something by Sigur Rós", "play anything by Μίκης Θεοδωράκης",
]


def corpus() -> list[str]:
    out: list[str] = []

    def add(s):
        if s not in out:
            out.append(s)

    for s in _test_literals():
        add(s)
    for s in EDGE:
        add(s)
    for f in FILLERS:
        for v in VERBS:
            for q in QUERIES:
                add(f + (f"{v} {q}" if v and q else v or q))
    for v in VERBS:
        for q in QUERIES[:8]:
            for t in TAILS:
                add((f"{v} {q}" if v else q) + t)
    return out


def _intent(i: ai.Intent) -> dict:
    return {"name": i.name, "query": i.query, "raw": i.raw, "extras": i.extras}


# ── py_re: Python's own answers for the constructs py_re.dart rewrites ──────
RE_CASES = [
    # (pattern, flags, subjects)
    (r"^\s*x\s*$", "", ["x", " x ", " x", "﻿x", "\u001cx", "\u0085x", "x\n", "x\n\n",
                        " x", "　x　"]),
    (r"^play$", "I", ["play", "PLAY", "pLaY", "play\n", "play\r", "plaıy", "plai"]),
    (r"^(?:is|kis|ss)$", "I", ["is", "İs", "ıS", "Kis", "ſs", "sſ", "IS",
                               "İſ"]),
    (r"^a.b$", "", ["a\nb", "a\rb", "a b", "a b", "a🎵b", "a\u0085b"]),
    (r"\b(it|that|them)\b", "I", ["play it", "play itself", "tilt", "Αit", "éit", "it's",
                                  "_it", "2it", "itα", "said it.", "THEM!", "ítem"]),
    (r"\b\w+\b", "", ["héllo wörld", "Αγάπη μου", "a_b c-d", "١٢٣ x", "xͅy", "日本語"]),
    (r"[^\w&']+", "", ["rock & roll", "guns n' roses", "AC/DC", "Sigur Rós", "x y"]),
    (r"^\s{0,3}#{1,6}\s*", "M", ["# h\n  ## g\n\n### k", "x\n# y", "   #### z\r\n# w"]),
    (r"^\s*[•\-\*]\s+", "M", ["• a\n- b\n * c\nd - e", "x\r\n- y"]),
    (r"[.!?]+\s*$", "", ["hi!!", "hi.\n", "hi?  ", "hi.\nx"]),
    (r"\s+(?:please|for\s+me)\s*$", "I", ["play x please", "play x PLEASE\n", "x for me"]),
]


def re_goldens() -> list[dict]:
    out = []
    for pat, flags, subjects in RE_CASES:
        fl = (re.I if "I" in flags else 0) | (re.M if "M" in flags else 0)
        rx = re.compile(pat, fl)
        for s in subjects:
            m = rx.match(s)
            se = rx.search(s)
            out.append({
                "pattern": pat, "flags": flags, "subject": s,
                "match": [m.start(), m.end()] if m else None,
                "search": [se.start(), se.end()] if se else None,
                "findall": rx.findall(s),
                "sub": rx.sub("_", s),
            })
    return out


MD = [
    "", "plain", "**Yesterday** — The Beatles", "Queued **12** matches for **radiohead**.",
    "### 🤖 Jarvis\n\n**🎵 Smart**\n• `play something chill` | x\n• `play similar` — y",
    "[Google AI Studio](https://aistudio.google.com/) under **Settings → AI Assistant**",
    "# Head\n  ## Sub\n- item one\n * item two\n__under__ and _it_ and `code`",
    "a    b\t\tc  \n  d", "*\"Where words fail, music speaks.\"*\n— **Hans Christian Andersen**",
    "nested [**bold link**](http://x) text", "[a](b) [c](d)", "x  [unclosed](link",
    "• bullet\n\n•  two\n   #not head", "snake_case_name and 2*3*4", "\r\n# r\r\n- s",
    "Did you mean **Time by Pink Floyd**, sir? (say 'no' for **Money** instead)",
]

ROWS = [
    {"path": "/m/a.flac", "title": "A", "artist": "X", "album": "Y", "genre": "rock",
     "year": 1999, "duration": 201.5, "format": "flac", "bpm": 120.0, "energy": 0.5,
     "track_num": 3, "count": 7, "last_played": "2026-11-01 10:00:00",
     "timbre": b"\x00\x01", "brightness": 0.3, "album_id": 4},
    {"path": "/m/b.mp3", "title": "B", "artist": None, "album": "", "genre": None,
     "year": None, "duration": 0.0, "timbre": None, "energy": 0},
    {"title": "only title", "extra": "dropped"},
]

NAME_CASES = [
    ("Artists similar to Radiohead include Muse, Coldplay, Thom Yorke and Portishead.",
     ["Muse", "Coldplay", "Radiohead", "Portishead", "Thom Yorke", "Blur", "Air"], ["Radiohead"]),
    ("Björk, Sigur Rós and múm are Icelandic; see also SIGUR ROS.",
     ["Sigur Rós", "Bjork", "Múm", "Sigur Ros", "GusGus"], []),
    ("Οι Πυξ Λαξ και ο Σωκράτης Μάλαμας μοιάζουν με τον ΜΑΛΑΜΑ",
     ["Πυξ Λαξ", "Σωκράτης Μάλαμας", "Μάλαμας", "ΣΩΚΡΑΤΗΣ ΜΑΛΑΜΑΣ"], []),
    ("Guns N' Roses, AC/DC, and Simon & Garfunkel; Earth, Wind & Fire",
     ["Guns N' Roses", "AC/DC", "Simon & Garfunkel", "Earth, Wind & Fire", "ACDC", "Wind"], []),
    ("Yes, Low and Air are bands; Various Artists too. The The is a band.",
     ["Yes", "Low", "Air", "Various Artists", "The The", "Band", "ABBA"], []),
    ("The page names Zebra first then Apple then Mango", ["Mango", "Apple", "Zebra", "Mango"], []),
    ("ﬁve ﬂowers ᾳδω KELVIN Korn 𝐁𝐨𝐥𝐝 Ⅻ", ["five flowers", "αιδω", "Korn", "Bold", "xii"], []),
    ("", ["Anything"], []), ("x", ["", "   ", "--", "abc"], ["ABC"]),
]


def fold_hashes(fn=None) -> list[int]:
    """FNV-1a (32-bit) over the UTF-8 of fn(chr(c)) + '\\x00' for each code
    point (default web_research._fold), one hash per 4096 code points
    (surrogates skipped)."""
    fn = fn or web_research._fold
    out = []
    h = 0x811C9DC5
    for o in range(sys.maxunicode + 1):
        if not (0xD800 <= o <= 0xDFFF):
            for b in fn(chr(o)).encode("utf-8") + b"\x00":
                h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
        if o % 4096 == 4095:
            out.append(h)
            h = 0x811C9DC5
    return out


def chat_memory_goldens() -> dict:
    """save_session / save_graph_state bytes with a fixed clock, and what
    load_session returns at three moments."""
    tmp = tempfile.mkdtemp()
    mgr = chat_memory.ChatMemoryManager()
    mgr._get_history_path = lambda: os.path.join(tmp, "chat_history.json")
    mgr._get_graph_state_path = lambda: os.path.join(tmp, "graph_state.json")
    messages = [{"sender": "user", "text": f"m{i}"} for i in range(55)] + [
        {"sender": "user", "text": "play Αγάπη μου 🎵"},
        {"sender": "assistant", "text": "Certainly. **Αγάπη μου** — Χ \"q\" \\ \t",
         "entities": {"track": {"path": "/m/a.flac", "title": "Αγάπη μου", "duration": 201.5,
                                "year": 1999, "bpm": None},
                      "artist": "Χ", "playlist": None, "intent": "play_now"},
         "intent": {"name": "play_now", "query": "αγάπη μου", "raw": "play Αγάπη μου 🎵",
                    "extras": {"agent": {"used_llm": True, "tools": ["play_music"]},
                               "options": [], "empty": {}}}},
    ]
    import time as _time
    real = _time.time
    try:
        chat_memory.time.time = lambda: 1764000000.25
        mgr.save_session(messages, True)
        mgr.save_graph_state(1216, 0)
        with open(os.path.join(tmp, "chat_history.json"), encoding="utf-8") as f:
            history = f.read()
        with open(os.path.join(tmp, "graph_state.json"), encoding="utf-8") as f:
            graph = f.read()
        loads = {}
        for label, t in (("fresh", 1764000000.25 + 900), ("expired", 1764000000.25 + 900.5)):
            chat_memory.time.time = lambda t=t: t
            s = mgr.load_session()
            loads[label] = {"messages": len(s["messages"]), "init_greeted": s["init_greeted"],
                            "file_left": os.path.exists(os.path.join(tmp, "chat_history.json"))}
    finally:
        chat_memory.time.time = real
    return {"messages": messages, "clock": 1764000000.25, "history_file": history,
            "graph_state_file": graph, "loads": loads}


def main():
    texts = corpus()
    data = {
        "intents": [_intent(ai.parse(t)) for t in texts],
        "clean_query": [[q, ai._clean_query(q)] for q in QUERIES + [
            " 'me a beatles' ", "something by stuff from x", "the the the", "songs",
            "a b c tracks.", "Album albums", "x , . !", "queue cue me x"]],
        "strip_markdown": [[t, llm_tools.strip_markdown(t)] for t in MD],
        "slim_track": [[{k: (None if isinstance(v, bytes) else v) for k, v in r.items()},
                        [k for k, v in r.items() if isinstance(v, bytes)],
                        llm_tools._slim_track(r)] for r in ROWS],
        "names_mentioned": [[t, n, e, web_research.names_mentioned(t, n, exclude=e)]
                            for t, n, e in NAME_CASES],
        "fold_hashes": fold_hashes(),
        "lower_hashes": fold_hashes(str.lower),
        "casefold_hashes": fold_hashes(str.casefold),
        "re": re_goldens(),
        "chat_memory": chat_memory_goldens(),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{OUT}: {len(texts)} utterances, {len(data['re'])} re cases")


if __name__ == "__main__":
    main()
