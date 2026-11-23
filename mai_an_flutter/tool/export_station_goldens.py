#!/usr/bin/env python3
"""Golden values for the stations port (PORT_MAP rule 3): what the Python
station code outputs, so the Dart port can be checked against it.

Two files:

  test/goldens/stations_synthetic.json   (committed)
      The taxonomy, credit keys and tag similarity over hand-picked strings,
      and an ArtistGraph + Drift over a small synthetic library (fixed RNG).
      No personal data.

  tools/state_backups/goldens/<bundle>/stations.json + library.db  (gitignored)
      20 fixed seeds from a real state bundle: the Play Similar walk and an
      auto-play-shaped station with accept/reject, plus the artist graph's
      per-artist culture and profile flags. The library.db beside it is the
      copy Python read (after its migrations), so the Dart test reads the same
      bytes. Run with:
          flutter test test/station_real_golden_test.dart

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_station_goldens.py            # newest bundle
    ~/miniconda3/bin/python tool/export_station_goldens.py path/to/bundle.zip
    ~/miniconda3/bin/python tool/export_station_goldens.py --synthetic-only
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import random
import shutil
import sys
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import artist_graph as ag  # noqa: E402
from utils import genre_taxonomy as gt  # noqa: E402
from utils.metadata_enrich import credit_keys, split_artist_credits  # noqa: E402


def _f(x) -> float:
    return float(round(float(x), 7))


# ── synthetic ────────────────────────────────────────────────────────────────

TAGS = [
    "", "rock", "Classic Rock", "alternative rock", "indierock", "hardrock",
    "hard rock", "newwave", "new wave", "postpunk", "punk", "shoegaze",
    "house", "deephouse", "techno", "entechno", "Έντεχνο", "laiko", "λαϊκό",
    "laïka", "rebetiko", "greek", "greekfolk", "greek pop", "pop", "dancepop",
    "synthpop", "folkpop", "kpop", "k-pop", "jpop", "mandopop", "hiphop",
    "hip hop", "alternativehiphop", "rap", "poprap", "trap", "drill", "grime",
    "rnb", "r&b", "rhythmandblues", "rhythm and blues", "soul", "neosoul",
    "funk", "jazz", "jazzfunk", "blues", "bluesrock", "country", "americana",
    "folk", "folkmetal", "metal", "heavymetal", "reggae", "reggaeton",
    "dancehall", "latin", "salsa", "bossanova", "fado", "morna", "worldmusic",
    "classical", "soundtrack", "filmscore", "disco", "gospel", "electronic",
    "electronica", "edm", "ambient", "triphop", "drumandbass", "dubstep",
    "experimental", "garage", "singersongwriter", "βραζιλιάνικο", "ελληνικά",
    "Musiques du monde", "Pop/Rock", "Électronique", "Métal",
]

NAMES = [
    "", "Björk", "Cesária Evora", "Cesária Évora", "Sigur Rós", "Mötley Crüe",
    "21 Savage & Metro Boomin", "Travis Scott/Metro Boomin/21 Savage",
    "163Margs, Digga D", "Russ Millions, YV, BUNI", "Dave feat. AJ Tracey",
    "Dave ft AJ Tracey", "Dave x AJ Tracey", "Earth, Wind & Fire",
    "Simon & Garfunkel", "Toquel, Light", "A + B", "Ab · Cd", "X & X",
    "Μαζωνάκης", "Γιώργος Μαζωνάκης", "ΜΑΡΙΝΕΛΛΑ", "Άλκηστις Πρωτοψάλτη",
    "Βασίλης Καρράς & Άννα Βίσση", "Trannos, Light", "Snik x Light",
    "Beyoncé featuring JAY-Z", "Ñengo Flow", "Łona", "Øyvind", "ﬁfty",
    "AC/DC", "Florence + the Machine", "Tyler, The Creator", "M83",
    "Café Tacvba", "Zoë", "İbrahim Tatlıses", "Straße", "  spaced  ",
    "A/B", "Jay - Z", "Μ.Β.", "ПРИВЕТ", "Ḿṏṍ", "ǅemal", "ΚΑΡΡΑΣ", "ΟΔΥΣΣΕΑΣ ΕΛΥΤΗΣ",
    "Йошкар", "が ガ", "방탄소년단", "Ｆｕｌｌ", "Ⅻ", "²Pac", "ℌello", "Æon", "x&y",
]

SCRIPT_TEXTS = ["", "Björk", "Αγάπη", "hello Ω", "ἄλφα", "ПРИВЕТ", "abc ϗ", "Ͱ"]


def synthetic() -> dict:
    out: dict = {}
    out["taxonomy"] = [
        {
            "tag": t,
            "bucket": gt.genre_bucket(t),
            "tokens": sorted(gt.genre_tokens(t)),
            "regional": gt.is_regional_tag(t),
            "display": gt.genre_display_label(t),
            "culture": gt.tag_culture(t),
            "culture_word": ag._is_culture_word(t),
        }
        for t in TAGS
    ]
    out["script_culture"] = [{"text": s, "culture": gt.script_culture(s)} for s in SCRIPT_TEXTS]
    out["credit_keys"] = [
        {"name": n, "split": split_artist_credits(n), "keys": sorted(credit_keys(n))}
        for n in NAMES
    ]
    out["artist_culture"] = [
        {"country": c, "genres": g, "names": n, "culture": ag.artist_culture(c, g, n)}
        for c, g, n in [
            (None, [], []), ("gr", [], []), ("US", ["laiko"], []),
            ("US", ["pop"], ["Μαζωνάκης"]), (" GB ", ["rock"], ["x", None]),
            (None, ["greekpop"], []), ("", [], ["", "abc"]),
        ]
    ]

    sim_tags = ["house", "deephouse", "techno", "electro", "laiko", "blues",
                "bluesrock", "rap", "trap", "garage", "pop", "dancepop", "rebetiko"]
    npmi = {"house|techno": 0.55, "dancepop|house": 0.42, "laiko|rebetiko": 0.7,
            "pop|rap": 0.05, "electro|house": 0.2}
    S = ag.tag_similarity(sim_tags, npmi)
    out["tag_similarity"] = {
        "tags": sim_tags, "npmi": npmi,
        "matrix": [[_f(v) for v in row] for row in S],
        "matrix_no_npmi": [[_f(v) for v in row] for row in ag.tag_similarity(sim_tags)],
    }

    out["library"] = _synthetic_library()
    out["library_no_tags"] = _synthetic_library(no_tags=True)
    return out


def _synthetic_library(no_tags: bool = False) -> dict:
    """A small library with every ArtistGraph case: tagged and untagged acts,
    a collab credit whose members are in the library, a namesake row, a hand-set
    row, Greek-script names and a regional scene."""
    rng = np.random.default_rng(7)
    D = 6
    acts = [
        # (artist, genres, country, manual, n_tracks, script names?)
        ("Inna", ["dancepop", "house", "pop"], "RO", False, 4, False),
        ("Alexandra Stan", ["dancepop", "pop"], "RO", False, 3, False),
        ("Disclosure", ["house", "deephouse", "electronic"], "GB", False, 3, False),
        ("Bicep", ["techno", "house"], "GB", False, 2, False),
        ("Fleetwood Mac", ["rock", "softrock", "pop"], "GB", False, 3, False),
        ("Gary Moore", ["blues", "bluesrock"], "GB", False, 2, False),
        ("Mazonakis", ["laiko", "pop", "greek"], "GR", False, 3, True),
        ("Kaiti Garbi", ["laiko", "greek pop"], None, False, 2, True),
        ("Light", ["progrock"], "GB", False, 3, False),          # namesake row
        ("Toquel", [], None, False, 2, False),                    # untagged
        ("Toquel, Light", ["shoegaze"], "GR", False, 2, False),   # collab credit row
        ("Snik", [], "GR", False, 2, False),
        ("Snik x Light", [], None, False, 1, False),
        ("Kendrick Lamar", ["hiphop", "rap", "conscioushiphop"], "US", False, 3, False),
        ("Dave", ["grime", "rap", "ukhiphop"], "GB", False, 2, False),
        ("Dave & AJ Tracey", [], None, False, 1, False),
        ("AJ Tracey", ["grime", "ukgarage"], "GB", False, 2, False),
        ("Hand Set", ["jazz"], "GR", True, 2, False),
        ("Nobody", [], None, False, 2, False),                   # no links at all
    ]
    meta, X, bpm, energy = [], [], [], []
    for k, (artist, genres, country, manual, n, greek) in enumerate(acts):
        centre = rng.normal(size=D)
        for t in range(n):
            meta.append({
                "artist": artist,
                "credit": artist,
                "genres": [] if no_tags else sorted({
                    "".join(c for c in g.lower() if c.isalnum()) for g in genres
                }),
                "country": country,
                "manual": manual,
                "names": [artist, f"Αλμπουμ {k}" if greek else f"Album {k}",
                          f"Track {k}.{t}"],
                "album": f"Album {k}" + ("" if t % 2 else "b"),
            })
            X.append(centre + 0.6 * rng.normal(size=D))
            # Every third act has no tempo: exercises the timbre fallback.
            bpm.append(0.0 if k % 3 == 2 else float(rng.uniform(70, 175)))
            energy.append(float(rng.uniform(0.02, 0.3)))
    X = np.array(X, dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    X = X.astype(np.float32)
    npmi = {"dancepop|house": 0.5, "deephouse|house": 0.6, "house|techno": 0.45,
            "grime|rap": 0.5, "hiphop|rap": 0.8, "laiko|pop": 0.1}

    g = ag.ArtistGraph(X, meta, npmi, credit_keys)
    graph = {
        "units": g.units,
        "unit_of": g.unit_of.tolist(),
        "culture": [c for c in g.culture],
        "inferred": g.inferred.tolist(),
        "namesake": g.namesake.tolist(),
        "has_tags": g.has_tags,
        "tags": g.tags,
        "same_act": {str(u): sorted(v) for u, v in g.same_act.items() if v},
        "collab": {str(u): sorted(v) for u, v in g.collab.items() if v},
        "W": [[_f(v) for v in row] for row in g.W],
        "rows": [[_f(v) for v in g.row(u)] for u in range(len(g))],
        "flavour_all": g.flavour(list(range(len(g)))),
    }

    artist_keys = [credit_keys(m["credit"]) for m in meta]
    albums = [m["album"] for m in meta]
    runs = []
    for seed in (0, 7, 18, 21, 27, len(meta) - 1):
        for focused in (False, True):
            for caps in ((2, 1, 0), (5, 1, 6)):
                d = ag.Drift(g, X, seed, artist_keys=artist_keys, album_keys=albums,
                             caps=ag.RollingCaps(*caps), focused=focused,
                             bpm=np.array(bpm), energy=np.array(energy))
                steps = []
                first = d.take(3)
                steps.append({"op": "take", "n": 3, "out": first, "flavour": d.flavour()})
                if first:
                    d.accept(first[0])
                    steps.append({"op": "accept", "idx": first[0]})
                    d.reject([first[-1]])
                    steps.append({"op": "reject", "idx": [first[-1]]})
                d.exclude([1, 2])
                steps.append({"op": "exclude", "idx": [1, 2]})
                more = d.take(6)
                steps.append({"op": "take", "n": 6, "out": more, "flavour": d.flavour()})
                rest = d.take(50)
                steps.append({"op": "take", "n": 50, "out": rest, "flavour": d.flavour()})
                runs.append({"seed": seed, "focused": focused, "caps": list(caps),
                             "steps": steps})
    return {
        "meta": meta,
        "X": [[float(v) for v in row] for row in X],
        "bpm": bpm,
        "energy": energy,
        "npmi": npmi,
        "graph": graph,
        "runs": runs,
    }


# ── real bundle ──────────────────────────────────────────────────────────────

SEEDS = 20
AUTOPLAY = dict(focused=True, max_per_artist=5, max_per_album=1, window=24)


async def real(bundle: str) -> str:
    from utils import track_graph as tg
    from utils.db_manager import DatabaseManager

    name = os.path.splitext(os.path.basename(bundle))[0]
    out_dir = os.path.join(REPO, "tools", "state_backups", "goldens", name)
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "library.db")
    if bundle.endswith(".zip"):
        with zipfile.ZipFile(bundle) as z, z.open("library.db") as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copyfile(bundle, db_path)

    db = DatabaseManager(db_path)
    await db.initialize()
    try:
        cg = await tg._coord_graph_cached(db)
        if cg is None:
            raise SystemExit("bundle has no coordinates")
        g = await tg._artist_graph_cached(db, cg)
        paths = cg["paths"]
        rng = random.Random(0)
        seeds = rng.sample(sorted(paths), SEEDS)

        cases = []
        for seed in seeds:
            similar = await tg.walk(db, seed, length=20)
            st = await tg.open_station(db, seed, avoid={seed}, **AUTOPLAY)
            steps = []
            a = await st.take(4)
            steps.append({"op": "take", "n": 4, "out": a, "flavour": st.flavour})
            if a:
                st.accept(a[0])
                steps.append({"op": "accept", "path": a[0]})
                st.reject([a[1]] if len(a) > 1 else [a[0]])
                steps.append({"op": "reject", "paths": [a[1]] if len(a) > 1 else [a[0]]})
            for _ in range(4):
                b = await st.take(4)
                steps.append({"op": "take", "n": 4, "out": b, "flavour": st.flavour})
            cases.append({"seed": seed, "walk": similar, "station": steps})

        units = []
        for u, name_ in enumerate(g.units):
            units.append({
                "name": name_, "culture": g.culture[u],
                "inferred": bool(g.inferred[u]), "namesake": bool(g.namesake[u]),
                "tracks": len(g.tracks_of[u]),
            })
        # A few affinity rows, to localise a divergence without the whole matrix.
        probe = sorted({int(g.unit_of[cg["path_to_idx"][s]]) for s in seeds})[:8]
        rows = {str(u): [_f(v) for v in g.row(u)] for u in probe}
        doc = {
            "bundle": os.path.basename(bundle),
            "db": "library.db",
            "paths": len(paths),
            "tags": g.tags,
            "has_tags": g.has_tags,
            "units": units,
            "rows": rows,
            "cases": cases,
        }
    finally:
        await db.close()
    out = os.path.join(out_dir, "stations.json")
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", nargs="?")
    ap.add_argument("--synthetic-only", action="store_true")
    args = ap.parse_args()

    syn = os.path.join(PORT, "test", "goldens", "stations_synthetic.json")
    os.makedirs(os.path.dirname(syn), exist_ok=True)
    with open(syn, "w") as f:
        json.dump(synthetic(), f, ensure_ascii=False, indent=1)
    print("wrote", os.path.relpath(syn, PORT))
    if args.synthetic_only:
        return

    bundle = args.bundle or max(
        glob.glob(os.path.join(REPO, "tools", "state_backups", "mai_an_lab_state_2*.zip")),
        key=os.path.getmtime,
    )
    print("wrote", os.path.relpath(asyncio.run(real(bundle)), PORT))


if __name__ == "__main__":
    main()
