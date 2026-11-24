"""Export Python reference values for Phase 6a (new_releases, text_match,
strip_markup) into test/goldens/new_releases.json."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "StreamripApp"))

from utils import new_releases as nr  # noqa: E402
from utils.text_match import levenshtein_distance  # noqa: E402
import re  # noqa: E402


def strip_markup(text: str) -> str:
    # Matches ui/widgets.py strip_markup without importing flet.
    return re.sub(r"\[/?[^\]]*\]", "", str(text))


NORM_CASES = [
    "",
    "Burial",
    "Ξεχωριστά (Deluxe Edition)",
    "ξεχωριστα",
    "Untrue [Remastered]",
    "Rossi.",
    "Björk - Homogenic - Deluxe Edition",
    "Track - Acoustic Version",
    "Album - 2024 Remaster",
    "Album - Normal Subtitle",
    "Café Tacvba (Live at MTV) [2020]",
    "ΟΔΟΣ ΣΤΡΑΤΟΥ",
    "  multiple   spaces\t&  punct!!! ",
    "21 Savage & Metro Boomin",
]

CANDIDATE_CASES = [
    "Mad Clip",
    "Simon & Garfunkel",
    "Billy Sio, Mad Clip",
    "Various Artists",
    "various",
    "Unknown Artist",
    "Unknown",
    "",
    "Travis Scott feat. Drake",
    "Travis Scott feat Drake",
    "Artist ft. Guest",
    "Artist FT Guest",
    "Artist x Other",
    "Artist X Other",
    "The xx",
    "Björk",
    "21 Savage",
]

LEVENSHTEIN_CASES = [
    ("", ""),
    ("abc", ""),
    ("", "abc"),
    ("kitten", "sitting"),
    ("haptik", "haptic"),
    ("equaliser", "equalizer"),
    ("björk", "bjork"),
    ("αβγ", "αβδ"),
    ("🎵a", "🎵b"),
    ("same", "same"),
]

STRIP_MARKUP_CASES = [
    "Plain text",
    "[b]Bold[/b] Title",
    "[color=#ff0000]Red[/color] & [i]Italic[/i]",
    "No closing [tag",
    "Nested [b][u]Both[/u][/b]!",
]


def main() -> None:
    out = {
        "norm": {s: nr.norm(s) for s in NORM_CASES},
        "is_lookup_candidate": {s: nr.is_lookup_candidate(s) for s in CANDIDATE_CASES},
        "levenshtein": [
            {"a": a, "b": b, "d": levenshtein_distance(a, b)}
            for a, b in LEVENSHTEIN_CASES
        ],
        "strip_markup": {s: strip_markup(s) for s in STRIP_MARKUP_CASES},
    }
    dest = os.path.join(
        os.path.dirname(__file__), "..", "test", "goldens", "new_releases.json"
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
