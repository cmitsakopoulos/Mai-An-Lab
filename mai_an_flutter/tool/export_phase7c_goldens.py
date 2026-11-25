#!/usr/bin/env python3
"""Golden values for phase 7c (PORT_MAP rule 3): the onboarding wizard's
finish (`OnboardingWizardModal._finish_onboarding`) — the config.toml
update it writes, the prefs it saves and the tab it lands on — for a few
choices of accent, startup screen, folder and streaming accounts.

    test/goldens/phase7c.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    PYTHONPATH=tool/.pydeps ~/miniconda3/bin/python tool/export_phase7c_goldens.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(HERE, ".pydeps"))
sys.path.insert(1, os.path.join(REPO, "StreamripApp"))

from ui.player import onboarding_modal as om  # noqa: E402

logging.disable(logging.CRITICAL)
OUT = os.path.join(PORT, "test", "goldens", "phase7c.json")

CASES = [
    {"name": "defaults"},
    {"name": "violet_search_creds", "accent": "#BF5AF2", "startup": "Search",
     "folder": "/storage/emulated/0/Music/Mine",
     "qobuz_user": " someone@example.com ", "qobuz_token": " tok123 ",
     "deezer_arl": "  arlvalue "},
    {"name": "jarvis_half_qobuz", "accent": "#30D158", "startup": "Jarvis",
     "folder": "/storage/emulated/0/Music", "qobuz_user": "12345", "qobuz_token": ""},
    {"name": "rose_library_deezer", "accent": "#FF375F", "startup": "Library",
     "folder": "/sdcard/Music/Rips", "deezer_arl": "abc"},
]


class _App:
    def __init__(self):
        self.page = None
        self.prefs = []
        self.restart = None

    def _save_pref(self, k, v):
        self.prefs.append([k, v])

    def restart_ui(self, target_tab=None, post_message=None):
        self.restart = {"target_tab": target_tab, "post_message": post_message}


def main():
    tmp = tempfile.mkdtemp(prefix="mai_an_7c_")
    om.get_app_dir = lambda: tmp
    import utils.streamrip_api as sa
    sa.get_config_path = lambda: os.path.join(tmp, "config.toml")
    out = {"accents": om.ACCENT_OPTIONS,
           "startups": [[k, l] for k, l, _ in om.STARTUP_OPTIONS], "cases": []}
    for c in CASES:
        captured = []
        om.update_config_params = lambda d: captured.append(json.loads(json.dumps(d)))
        app = _App()
        w = om.OnboardingWizardModal(app)
        w._dialog = None
        if "accent" in c:
            w.selected_accent = c["accent"]
        if "startup" in c:
            w.selected_startup = c["startup"]
        w.detected_music_dir = c.get("folder", "/storage/emulated/0/Music/Streamrip")
        w.qobuz_user = c.get("qobuz_user", "")
        w.qobuz_token = c.get("qobuz_token", "")
        w.deezer_arl = c.get("deezer_arl", "")
        w._finish_onboarding(None)
        out["cases"].append({
            "name": c["name"], "input": c,
            "update": captured[0], "prefs": app.prefs, "restart": app.restart,
            "marker": open(os.path.join(tmp, ".onboarded")).read(),
        })
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
