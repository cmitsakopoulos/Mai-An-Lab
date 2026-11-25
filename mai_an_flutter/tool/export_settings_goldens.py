#!/usr/bin/env python3
"""Golden values for the port's Settings search (PORT_MAP rule 3): the rows
settings.py `_render_hub_or_search` lists for each query, in order, run on
the Flet code itself. Committed:

  test/goldens/settings_search.json
      queries   each query and the titles of the rows it lists, keeping
                only the groups the port has (the others are not in its
                hub; dropping them leaves the order of the rest unchanged,
                as each row is matched and scored on its own).

    flutter test test/settings_search_test.dart

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_settings_goldens.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(HERE, ".pydeps"))
sys.path.insert(1, os.path.join(REPO, "StreamripApp"))

import flet as ft  # noqa: E402
from ui.views import settings as S  # noqa: E402
from ui.widgets import HubSettingItem  # noqa: E402

PORTED = {
    "Companion & Sync", "Storage & Paths", "Authentication",
    "AI Assistant (Jarvis)", "Appearance",
    "Audio & DSP", "Haptic Feedback", "Metadata", "Permissions",
    "Database Management",
    "Advanced", "About",
}

QUERIES = [
    "eq", "equalizer", "equaliser", "bass", "eq bass", "wipe", "wipe db",
    "folder", "music folder", "path", "download", "libary", "library path",
    "accent", "colour", "color", "theme", "dark mode", "startup", "sort",
    "pill", "landing", "most played", "haptic", "haptics", "vibration",
    "vibraton", "intensity", "drag", "permission", "permissions",
    "microphone", "mic", "storage", "database", "databse", "clear", "cache",
    "clear cache", "reset", "toml", "config", "raw config", "export",
    "import", "about", "version", "github", "credits", "dsp", "dynamism",
    "boost", "loudness", "jarvis", "qobuz", "token", "metadata", "sync",
    "turn off haptics", "enable eq", "disable", "change folder",
    "set path", "a", "zz", "xyzzy", "general", "customisation",
    "developer tools", "DEVELOPER", "  Eq  ", "pca", "index", "sqlite",
    "notification", "audio", "sound", "volume", "preset", "curve", "tabs",
    "glow", "top artists", "onboarding", "demo", "lobotomize",
    "companion", "pairing", "pin", "mac", "offload", "wireless", "server",
    "pair with mac", "workbench", "musicbrainz", "genre", "artist tags",
    "auto-play", "mbid", "tagger",
]


class _App:
    def safe_update(self, fn):
        fn()


def matches(view, query):
    view._render_hub_or_search(query.strip().lower())
    titles = []
    for c in view._scroll_column.controls:
        if isinstance(c, HubSettingItem):
            row = c.content.content.content  # ScaleButton → Container → card → Row
            titles.append(row.controls[1].controls[0].value)
    return titles


def main():
    v = S.SettingsView.__new__(S.SettingsView)
    v.app = _App()
    v._search_bar_container = ft.Container()
    v._scroll_column = ft.Column()
    v._search_registry = S.SettingsView._build_search_registry(v)
    out = []
    for q in QUERIES:
        out.append({"query": q,
                    "titles": [t for t in matches(v, q) if t in PORTED]})
    path = os.path.join(PORT, "test", "goldens", "settings_search.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"queries": out}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"wrote {path}: {len(out)} queries")


if __name__ == "__main__":
    main()
