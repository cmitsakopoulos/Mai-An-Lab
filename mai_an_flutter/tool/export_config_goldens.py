#!/usr/bin/env python3
"""Golden values for the port's config.toml writer (PORT_MAP rule 3): what
utils/streamrip_api.py `update_config_params` leaves in the file after each of
the Settings page's writes, byte for byte. No personal data, so the output is
committed:

  test/goldens/config_synthetic.json
      flows   Settings' own sequences (settings.py: appearance, general,
              paths, DSP switches, EQ presets saved, picked and deleted,
              haptics, demo search), each run on the template config
              (utils/config.toml, as ensure_config_exists writes it) and on
              the newest phone bundle's config.toml with every credential
              blanked. Each step: the params as update_config_params got
              them (tomlkit items unwrapped) and the whole file after.
      edges   small hand-written files through the same function, without
              ensure_config_exists (its template repair would fill them in):
              comments, indents, CRLF, missing trailing newlines, multi-line
              values, quoted keys, sub-tables, inline and super-table
              custom_presets, string escapes, float spellings.

    flutter test test/config_file_test.dart

tomlkit is pinned to the version the Android build bundles (0.15.2, see
StreamripApp/build/site-packages), installed apart from the dev env (which
has streamrip's 0.7.2):

    ~/miniconda3/bin/pip install --target tool/.pydeps tomlkit==0.15.2

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_config_goldens.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(HERE, ".pydeps"))
sys.path.insert(1, os.path.join(REPO, "StreamripApp"))

import tomlkit  # noqa: E402

TK_VERSION = "0.15.2"
assert tomlkit.__version__ == TK_VERSION, (
    f"tomlkit {tomlkit.__version__}; install {TK_VERSION} into tool/.pydeps")

_TMP = tempfile.mkdtemp()
CONFIG = os.path.join(_TMP, "config.toml")
os.environ["STREAMRIP_CONFIG_PATH"] = CONFIG

from utils import streamrip_api as api  # noqa: E402
from utils.config import BLANK_CONFIG_PATH  # noqa: E402

_real_ensure = api.ensure_config_exists


def plain(v):
    if hasattr(v, "unwrap"):
        return v.unwrap()
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


class Run:
    """One file, written by a sequence of update_config_params calls."""

    def __init__(self, name, src, repair=True):
        self.name, self.src, self.steps = name, src, []
        with open(CONFIG, "w", encoding="utf-8", newline="") as f:
            f.write(src)
        api.ensure_config_exists = _real_ensure if repair else (lambda: None)
        if repair:
            # A complete config passes ensure_config_exists untouched; the
            # port relies on that (it does not port the repair yet).
            _real_ensure()
            assert self.text() == src, f"{name}: ensure_config_exists rewrote it"

    def text(self):
        with open(CONFIG, encoding="utf-8", newline="") as f:
            return f.read()

    def update(self, params):
        recorded = json.loads(json.dumps(plain(params)))
        assert api.update_config_params(params), params
        self.steps.append({"params": recorded, "out": self.text()})

    def load(self):
        return api.load_config()

    def dump(self):
        return {"name": self.name, "src": self.src, "steps": self.steps}


# settings.py, one function per write path ---------------------------------

def save_appearance(r, accent="#FFD60A", pill="category", jarvis=True,
                    network=False, playlists=True, genres=True, artists=True,
                    albums=False, tracks=True, landing=(True,) * 5,
                    startup="Library", sort="date"):
    r.update({
        "appearance": {
            "accent_color": accent, "pill_style": pill,
            "show_jarvis": jarvis, "show_network": network,
            "show_playlists": playlists, "show_genres": genres,
            "show_artists": artists, "show_albums": albums,
            "show_tracks": tracks,
        },
        "landing": {
            "show_search_history": landing[0], "show_library_stats": landing[1],
            "show_top_artists": landing[2], "show_top_genres": landing[3],
            "show_new_releases": landing[4],
        },
        "general": {"startup_page": startup, "default_sort": sort},
    })


def save_general(r, startup, sort):
    r.update({"general": {"startup_page": startup, "library_sort": sort}})


def save_paths(r, dl, quality):
    r.update({"downloads": {"folder": dl}})
    r.update({"general": {"default_quality": quality}})


def save_preset_gains(r, name, gains):
    cp = r.load().get("dsp", {}).get("custom_presets", {})
    cp[name] = gains
    r.update({"dsp": {"custom_presets": cp}})


def slider_moved(r, gains, was_custom):
    if not was_custom:
        r.update({"dsp": {"active_preset": "Custom"}})
    save_preset_gains(r, "Custom", gains)


def save_named_preset(r, name, gains):
    save_preset_gains(r, name, gains)
    r.update({"dsp": {"active_preset": name}})


def delete_preset(r, name):
    dsp = r.load().get("dsp", {})
    cp = dsp.get("custom_presets", {})
    if name in cp:
        del cp[name]
    active = dsp.get("active_preset", "Flat")
    r.update({"dsp": {"custom_presets": cp,
                      "active_preset": "Flat" if active == name else active}})


def settings_flow(r):
    save_appearance(r, accent="#30D158", pill="unified", albums=True,
                    landing=(True, False, True, True, False), startup="Search")
    save_general(r, "Search", "artist")
    save_paths(r, "/storage/emulated/0/Music", "")
    save_paths(r, "/storage/emulated/0/Music/Mai \"An\" Lab é", "hires")
    r.update({"downloads": {"folder": "/storage/emulated/0/Download"}})
    r.update({"dsp": {"dynamism_enabled": True}})
    r.update({"dsp": {"equalizer_enabled": True}})
    r.update({"dsp": {"active_preset": "Rock"}})
    slider_moved(r, [4.0, 2.0, -2.0, 2.0, 4.5], was_custom=False)
    slider_moved(r, [4.0, 2.0, -2.0, 0.1, 4.5], was_custom=True)
    save_named_preset(r, "Late Night", [1.5, 0.0, -0.5, 2.0, 3.0])
    save_named_preset(r, "Zé 2", [-15.0, 15.0, 0.30000000000000004, 1e-05, 0.0])
    save_named_preset(r, "Late Night", [1.0, 1.0, 1.0, 1.0, 1.0])
    r.update({"dsp": {"active_preset": "Custom"}})
    delete_preset(r, "Late Night")
    delete_preset(r, "Zé 2")
    delete_preset(r, "Custom")
    r.update({"dsp": {"equalizer_enabled": False, "dynamism_enabled": False,
                      "active_preset": "Flat"}})
    r.update({"haptics": {"haptic_feedback_enabled": False}})
    r.update({"haptics": {
        "eq_drag_intensity": "none", "swipe_queue_intensity": "vibrate",
        "long_press_intensity": "light", "network_tap_intensity": "selection",
        "network_reseed_intensity": "heavy", "network_walk_intensity": "medium",
    }})
    r.update({"appearance": {"demo_search": True}})
    save_appearance(r)


SECRETS = {
    "qobuz": {"email_or_userid": "", "password_or_token": "", "app_id": "",
              "secrets": []},
    "tidal": {"user_id": "", "access_token": "", "refresh_token": ""},
    "deezer": {"arl": ""},
    "soundcloud": {"client_id": ""},
    "assistant": {"gemini_api_key": ""},
}


def phone_config():
    bundles = sorted(glob.glob(os.path.join(
        REPO, "tools", "state_backups", "mai_an_lab_state_2*.zip")))
    if not bundles:
        return None, None
    with zipfile.ZipFile(bundles[-1]) as z:
        text = z.read("config.toml").decode("utf-8")
    doc = tomlkit.parse(text)
    for section, keys in SECRETS.items():
        for k, v in keys.items():
            if section in doc and k in doc[section]:
                doc[section][k] = v
    return os.path.basename(bundles[-1]), tomlkit.dumps(doc)


EDGES = [
    ("comment after the last key",
     '[a]\nx = 1\n# tail comment\n\n[b]\ny = 2\n', [{"a": {"z": 3}}]),
    ("comment then header",
     '[a]\nx = 1\n# tail comment\n[b]\ny = 2\n', [{"a": {"z": 3}}]),
    ("no trailing newline, key", '[a]\nx = 1', [{"a": {"z": 3}}]),
    ("no trailing newline, table", '[a]\nx = 1', [{"b": {"z": 3}}]),
    ("trailing blank lines, table", '[a]\nx = 1\n\n\n', [{"b": {"z": 3}}]),
    ("trailing blank lines, key", '[a]\nx = 1\n\n\n', [{"a": {"z": 3}}]),
    ("empty file", '', [{"dsp": {"x": 1, "y": "s"}}, {"b": {}}]),
    ("indented keys", '[a]\n  x = 1\n  y = 2\n', [{"a": {"z": 3, "y": 5}}]),
    ("multi-line array replaced", '[a]\nx = [\n  1, # one\n  "]",\n]\ny = 2\n',
     [{"a": {"x": [3.5]}}]),
    ("multi-line strings", '[a]\nx = """\nab"c""""\ny = \'\'\'q\'\'\'\nz = 1\n',
     [{"a": {"x": "s", "y": True, "w": 0}}]),
    ("quoted key", '[a]\n"x" = 1\n\'y z\' = 2\n', [{"a": {"x": 2, "y z": 3}}]),
    ("spaced header", '[ a ]\nx = 1\n', [{"a": {"x": 2, "w": 1}}]),
    ("comment after a value", '[a]\nx = 1 # c\ny="v"#d\n',
     [{"a": {"x": "s", "y": False}}]),
    ("no spaces around =", 'x = 1\n[a]\ny=1\n', [{"a": {"q": True, "y": 2}}]),
    ("only a sub-table", '[a.b]\nx = 1\n', [{"a": {"q": True}}]),
    ("table with a sub-table",
     '[a]\nx = 1\n[a.b]\ny = 1\n', [{"a": {"x": 5}}, {"a": {"q": True}}]),
    ("literal string replaced", "[a]\nx = 'lit'\n", [{"a": {"x": "new"}}]),
    ("CRLF", '[a]\r\nx = 1\r\ny = 2\r\n', [{"a": {"z": 3, "x": 0}}]),
    ("string escapes", '[a]\nx = 1\n',
     [{"a": {"s": "\x01\x1b\x7f\t\b\f\r\n\"\\ é😀/"}}]),
    ("floats", '[a]\nx = 1\n',
     [{"a": {"f": [-0.0, 1e300, 5e-324, 123456789.0,
                   1234567890123456789.0, 1e16, 1e-4, 9.99e-5, 0.1]}}]),
    ("empty list, negative int", '[a]\nx = 1\n', [{"a": {"e": [], "n": -5}}]),
    ("root keys before the first table", 'a = 1\n\n[t]\nb = 2\n',
     [{"t": {"c": 3}}, {"u": {"d": 4}}]),
    ("dotted key in the table", '[a]\nb.c = 1\nd = 2\n', [{"a": {"d": 3, "e": 4}}]),
]

PRESET_SOURCES = [
    ("inline presets",
     '[dsp]\nactive_preset = "Custom"\n'
     'custom_presets = {Custom = [1.8, 0.02], "My P" = [1.0], B = [2.0]}\n'
     '\n[general]\nx = 1\n'),
    ("inline presets, one entry",
     '[dsp]\ncustom_presets = {Custom = [1.8973214285714626, 0.022321428571462576]}\n'),
    ("inline presets, padded",
     '[dsp]\ncustom_presets = { Custom = [1.0] , B = [ 2.0 ] }  # mine\nz = 1\n'),
    ("inline presets, empty", '[dsp]\ncustom_presets = {}\nz = 1\n'),
    ("inline presets, empty padded", '[dsp]\ncustom_presets = { }\n'),
    ("super-table presets",
     '[dsp]\nactive_preset = "Custom"\n\n[dsp.custom_presets]\n'
     'Custom = [1.8, 0.02]\n"My P" = [1.0]\nB = [2.0]\n[general]\nx = 1\n'),
    ("no presets, blank before next table",
     '[dsp]\nactive_preset = "Flat"\n\n[general]\nx = 1\n'),
    ("no presets, next table adjacent",
     '[dsp]\nactive_preset = "Flat"\n[general]\nx = 1\n'),
    ("no presets, last table", '[general]\nx = 1\n[dsp]\nactive_preset = "Flat"\n'),
    ("no dsp table", '[general]\nx = 1\n'),
]


def preset_edges():
    out = []
    for name, src in PRESET_SOURCES:
        for label, fn in [
            ("set Custom", lambda r: save_preset_gains(r, "Custom", [0.5, -1.0])),
            ("add a preset", lambda r: save_preset_gains(r, "Zé Name", [0.5])),
            ("add two", lambda r: (save_preset_gains(r, "C1", [1.0]),
                                   save_preset_gains(r, "C2", [2.0]))),
            ("delete the middle", lambda r: delete_preset(r, "My P")),
            ("delete the last", lambda r: delete_preset(r, "B")),
            ("delete the first", lambda r: delete_preset(r, "Custom")),
            ("delete all", lambda r: [delete_preset(r, n)
                                      for n in ("Custom", "My P", "B")]),
            ("delete absent", lambda r: delete_preset(r, "Nope")),
        ]:
            r = Run(f"{name}: {label}", src, repair=False)
            fn(r)
            out.append(r.dump())
    return out


def main():
    flows = []
    with open(BLANK_CONFIG_PATH, encoding="utf-8") as f:
        template = f.read()
    # ensure_config_exists: the template, its folder emptied, re-dumped.
    os.remove(CONFIG) if os.path.exists(CONFIG) else None
    _real_ensure()
    with open(CONFIG, encoding="utf-8", newline="") as f:
        fresh = f.read()
    assert fresh == template, "ensure_config_exists changed the template"
    r = Run("template", fresh)
    settings_flow(r)
    flows.append(r.dump())

    bundle, phone = phone_config()
    if phone is not None:
        r = Run(f"phone config ({bundle}), credentials blanked", phone)
        settings_flow(r)
        flows.append(r.dump())
    else:
        print("no phone bundle in tools/state_backups: phone flow skipped")

    edges = []
    for name, src, steps in EDGES:
        r = Run(name, src, repair=False)
        for p in steps:
            r.update(p)
        edges.append(r.dump())
    edges += preset_edges()

    path = os.path.join(PORT, "test", "goldens", "config_synthetic.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"tomlkit": TK_VERSION, "template": template, "flows": flows,
                   "edges": edges}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    n = sum(len(c["steps"]) for c in flows + edges)
    print(f"wrote {path}: {len(flows)} flows, {len(edges)} edge files, {n} writes")


if __name__ == "__main__":
    main()
