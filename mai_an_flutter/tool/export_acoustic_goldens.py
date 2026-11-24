#!/usr/bin/env python3
"""Golden values for the acoustic build (PORT_MAP rule 3): what the Flet
app's `track_graph.build_acoustic_edges` (with pca_engine's cleaver and
genre_similarity's NPMI model) writes, so the Dart port can be checked.

Each scenario runs Python's own builder on a fresh database and records:
the return value, `redundant_raw_features` (with ties pinned, see
`_symmetric_redundant`), the matrix that entered the SVD
(numpy's float32 Z, as an FNV-1a hash of its bytes, plus its shape), the
singular values (numpy's float32 SVD, and a float64 SVD of the same Z), every stored `pca_coords` afterwards, and the stored
`genre_affinity.model` text.

Two files:

  test/goldens/acoustic_synthetic.json   (committed)
      Synthetic libraries (`SCENARIOS`): correlated scalars (a three-way
      and a two-way cluster), stale and malformed features, rows outside
      the library, constant columns, tiny libraries, the z_score and
      scalar_weight variants, and an enrichment corpus with Greek, empty,
      malformed and non-ok rows. Each scenario's setup statements are in
      the file, so the Dart test builds the same database. No personal data.

  tools/state_backups/goldens/<bundle>/acoustic.json   (gitignored)
      The same, on a copy of the real library.db that
      export_station_goldens.py put there (run that first).
          flutter test test/acoustic_real_golden_test.dart

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_acoustic_goldens.py
    ~/miniconda3/bin/python tool/export_acoustic_goldens.py --synthetic-only
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sqlite3
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import pca_engine  # noqa: E402
from utils import track_graph as tg  # noqa: E402
from utils.db_manager import DatabaseManager  # noqa: E402
from utils.pca_engine import redundant_raw_features  # noqa: E402

logging.disable(logging.CRITICAL)

DIMS = 68


def fnv1a64(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


# ── Capturing numpy's SVD input and output ──────────────────────────────────
_CAPTURE: dict = {}
_svd = np.linalg.svd


def _capturing_svd(a, full_matrices=True, *args, **kwargs):
    out = _svd(a, full_matrices, *args, **kwargs)
    _CAPTURE["z"] = np.array(a)
    _CAPTURE["s"] = np.array(out[1])
    return out


np.linalg.svd = _capturing_svd


# ── The cleaver's ties ──────────────────────────────────────────────────────
# In a two-scalar cluster both scalars' centrality is the same |r|, but
# np.corrcoef divides it by the two deviations in opposite orders, so the
# two entries can differ in the last bit, and that bit (BLAS noise: it
# differs between numpy builds) decides which scalar stays. The goldens pin
# the rule instead: Python's own cleaver given the exactly symmetric
# (C + Cᵀ) / 2, where a tie keeps the lower index. `redundant_numpy` keeps
# what unpatched numpy chose here, for the record.
_redundant_from = pca_engine._redundant_features_from


def _symmetric_redundant(corr, threshold=0.70):
    c = np.asarray(corr, dtype=float)
    return _redundant_from((c + c.T) / 2, threshold)


def _numpy_redundant(rows) -> list:
    pca_engine._redundant_features_from = _redundant_from
    try:
        return sorted(redundant_raw_features(rows))
    finally:
        pca_engine._redundant_features_from = _symmetric_redundant


pca_engine._redundant_features_from = _symmetric_redundant


# ── Synthetic libraries ─────────────────────────────────────────────────────

def _blob(v) -> bytes:
    return np.asarray(v, dtype="<f4").tobytes()


def _library(n: int, seed: int, *, constant: bool = False, extras: bool = True):
    """Setup statements for an n-track library with v5 features.

    Scalars are built to correlate: bpm and brightness follow energy (one
    three-way cluster, energy central), spectral_flatness follows rolloff
    (a two-way cluster). `constant` holds beat_strength and
    spectral_contrast fixed across the library."""
    rng = np.random.default_rng(seed)
    centres = rng.normal(0, 1.5, (3, DIMS))
    stmts: list = []
    n_artists = 12
    for a in range(1, n_artists + 1):
        name = ["Άλφα", "Beta", "Γάμμα", "Delta", "Echo", "Foxtrot", "Golf",
                "Hotel", "India", "Juliet", "Kilo", "Lima"][a - 1]
        stmts.append(("INSERT INTO artists (id, name) VALUES (?, ?)", [a, name]))
        stmts.append((
            "INSERT INTO albums (id, artist_id, title, genre) VALUES (?, ?, ?, ?)",
            [a, a, f"Album {a}", "Rock"],
        ))
    for i in range(n):
        path = f"/m/{i % n_artists}/{i:03d} τραγούδι.flac"
        stmts.append((
            "INSERT INTO tracks (album_id, title, path, format, added_date) "
            "VALUES (?, ?, ?, 'FLAC', ?)",
            [i % n_artists + 1, f"Track {i}", path, 1700000000 + i],
        ))
        c = centres[i % 3]
        timbre = (c + rng.normal(0, 0.6, DIMS)).astype(np.float32)
        energy = float(rng.uniform(0.15, 0.95))
        bpm = 60 + 110 * energy + float(rng.normal(0, 6))
        if i % 23 == 5:
            bpm = 0.0          # log2(max(bpm, 1)) = 0
        elif i % 29 == 7:
            bpm = 0.5
        brightness = 0.7 * energy + float(rng.normal(0, 0.04))
        rolloff = float(rng.uniform(0.1, 0.9))
        flatness = 0.8 * rolloff + float(rng.normal(0, 0.05))
        beat = 0.5 if constant else float(rng.uniform(0, 1))
        contrast = 0.37 if constant else (
            None if i % 17 == 3 else float(rng.uniform(0, 1)))
        stmts.append((
            "INSERT INTO play_counts (track_path, count, bpm, energy, "
            "brightness, rolloff, beat_strength, spectral_flatness, "
            "spectral_contrast, key_index, timbre, features_version) "
            "VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 5)",
            [path, bpm, energy, brightness, rolloff, beat, flatness, contrast,
             i % 24, {"blob": _blob(timbre).hex()}],
        ))
    if extras:
        # Rows the build must skip or leave alone.
        for j, (version, blob, coords) in enumerate([
            (4, _blob(np.ones(DIMS)), [9.0, 9.0, 9.0]),   # stale: keeps coords
            (4, _blob(np.ones(DIMS)), None),
            (None, _blob(np.ones(DIMS)), None),           # no version
            (5, _blob(np.ones(DIMS - 4)), None),          # short BLOB
            (5, _blob(np.ones(DIMS + 1)), None),          # long BLOB
            (5, None, None),                              # no timbre
        ]):
            path = f"/m/extra/{j}.flac"
            stmts.append((
                "INSERT INTO tracks (album_id, title, path, format, added_date) "
                "VALUES (1, ?, ?, 'FLAC', 1690000000)",
                [f"Extra {j}", path],
            ))
            stmts.append((
                "INSERT INTO play_counts (track_path, count, bpm, energy, "
                "timbre, features_version, pca_coords) "
                "VALUES (?, 0, 120, 0.5, ?, ?, ?)",
                [path, None if blob is None else {"blob": blob.hex()}, version,
                 None if coords is None else {"blob": _blob(coords).hex()}],
            ))
        # Features for a file no longer in the library.
        stmts.append((
            "INSERT INTO play_counts (track_path, count, bpm, energy, timbre, "
            "features_version) VALUES ('/m/gone.flac', 0, 120, 0.5, ?, 5)",
            [{"blob": _blob(np.ones(DIMS)).hex()}],
        ))
        # A track never analysed.
        stmts.append((
            "INSERT INTO tracks (album_id, title, path, format, added_date) "
            "VALUES (2, 'Unplayed', '/m/extra/unplayed.flac', 'FLAC', 1690000001)",
            [],
        ))
    return stmts


def _g(*names):
    return json.dumps([{"name": n, "count": 1} for n in names], ensure_ascii=False)


ENRICHMENT = [
    ("Άλφα", _g("Hip Hop", "Trap", "Rap"), "ok"),
    ("Beta", _g("hip hop", "trap"), "ok"),
    ("Γάμμα", _g("Λαϊκό", "Έντεχνο", "Rebetiko"), "ok"),
    ("Delta", _g("λαϊκό", "έντεχνο"), "lowconfidence"),
    ("Echo", _g("rock", "hard rock", "metal"), "ok"),
    ("Foxtrot", _g("Rock", "Hard-Rock", "blues"), "ok"),
    ("Golf", _g("rock", "metal", "Nu Metal"), "ok"),
    ("Hotel", _g("jazz", "blues", "rebetiko"), "ok"),
    ("India", _g("jazz", "blues"), "ok"),
    ("Juliet", _g("techno", "house"), "notfound"),      # not ok: ignored
    ("Kilo", "not json", "ok"),                          # malformed
    ("Lima", _g("--", "!!"), "ok"),                      # empty tokens
    ("Mike", "[]", "ok"),
    ("November", None, "ok"),
    ("Oscar", _g("techno", "house", "rock"), "ok"),
    ("Papa", _g("techno", "house", "electro"), "ok"),
    ("Quebec", _g("rock", "blues", "hard rock"), "ok"),
    ("Romeo", _g("trap", "rap", "hip hop", "drill"), "ok"),
]


def _enrichment():
    return [(
        "INSERT INTO artist_enrichment (artist_name, genres, status) "
        "VALUES (?, ?, ?)", [a, g, s],
    ) for a, g, s in ENRICHMENT]


SETUPS = {
    "library": lambda: _library(140, 7) + _enrichment(),
    "small": lambda: _library(30, 11) + _enrichment(),
    "constant": lambda: _library(60, 13, constant=True, extras=False),
    "three": lambda: _library(3, 17, extras=False),
    "two": lambda: _library(2, 19, extras=False),
    "one": lambda: _library(1, 23),
    "empty": lambda: _enrichment(),
}

SCENARIOS = [
    # name, setup, kwargs
    ("library", "library", {}),
    ("library_unweighted", "library", {"scalar_weight": 1.0}),
    ("library_centred", "library", {"z_score": False}),
    ("small", "small", {}),
    ("constant", "constant", {}),
    ("three", "three", {}),
    ("two", "two", {}),
    ("one", "one", {}),
    ("empty", "empty", {}),
]


# ── Running the build ───────────────────────────────────────────────────────

def _arg(v):
    if isinstance(v, dict):
        return bytes.fromhex(v["blob"])
    return v


def _coords(con) -> dict:
    return {
        p: [float(x) for x in np.frombuffer(b, "<f4")]
        for p, b in con.execute(
            "SELECT track_path, pca_coords FROM play_counts "
            "WHERE pca_coords IS NOT NULL ORDER BY track_path")
    }


async def _build(db_path: str, kwargs: dict) -> dict:
    _CAPTURE.clear()
    m = DatabaseManager(db_path)
    await m.initialize()
    rows = await m.get_tracks_with_features(tg.FEATURES_VERSION)
    redundant = sorted(redundant_raw_features(rows))
    redundant_numpy = _numpy_redundant(rows)
    n = await tg.build_acoustic_edges(m, **kwargs)
    await m.close()
    con = sqlite3.connect(db_path)
    model = con.execute("SELECT model FROM genre_affinity WHERE id = 1").fetchone()
    out = {
        "kwargs": kwargs,
        "returned": n,
        "redundant": redundant,
        "redundant_numpy": redundant_numpy,
        "z_shape": None,
        "z_fnv": None,
        "singular_values": None,
        "coords": _coords(con),
        "model": model[0] if model else None,
    }
    con.close()
    if "z" in _CAPTURE:
        z = _CAPTURE["z"]
        assert z.dtype == np.float32
        out["z_shape"] = list(z.shape)
        out["z_fnv"] = fnv1a64(np.ascontiguousarray(z).astype("<f4").tobytes())
        out["singular_values"] = [float(s) for s in _CAPTURE["s"]]
        # The same Z through a float64 SVD: what the port's float64 solver
        # should reproduce to rounding.
        s64 = _svd(z.astype(np.float64), compute_uv=False)
        out["singular_values_f64"] = [float(s) for s in s64]
    return out


def synthetic() -> dict:
    setups = {name: make() for name, make in SETUPS.items()}
    scenarios = {}
    for name, setup, kwargs in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "library.db")
            m = DatabaseManager(db)
            asyncio.run(m.initialize())
            asyncio.run(m.close())
            con = sqlite3.connect(db)
            for sql, args in setups[setup]:
                con.execute(sql, [_arg(a) for a in args])
            con.commit()
            con.close()
            res = asyncio.run(_build(db, kwargs))
        res["setup"] = setup
        scenarios[name] = res
        print(f"  {name}: {res['returned']} tracks, "
              f"{len(next(iter(res['coords'].values()), []))} dims, "
              f"cleaved {res['redundant']}"
              + ("" if res['redundant'] == res['redundant_numpy']
                 else f" (unpatched numpy: {res['redundant_numpy']})"))
    return {
        "setups": {k: [[sql, args] for sql, args in v] for k, v in setups.items()},
        "scenarios": scenarios,
    }


def real(goldens_dir: str) -> dict:
    src = os.path.join(goldens_dir, "library.db")
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "library.db")
        con = sqlite3.connect(src)
        con.execute(f"VACUUM INTO '{db}'")
        con.close()
        res = asyncio.run(_build(db, {}))
    print(f"  real: {res['returned']} tracks, cleaved {res['redundant']}"
          f" (unpatched numpy: {res['redundant_numpy']}), Z {res['z_shape']}")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic-only", action="store_true")
    ap.add_argument("--goldens", help="a tools/state_backups/goldens/<bundle> dir")
    a = ap.parse_args()
    path = os.path.join(PORT, "test", "goldens", "acoustic_synthetic.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(synthetic(), f, ensure_ascii=False)
    print(f"wrote {path}")
    if a.synthetic_only:
        return
    gd = a.goldens
    if not gd:
        dirs = sorted(
            d for d in glob.glob(os.path.join(REPO, "tools", "state_backups",
                                              "goldens", "mai_an_lab_state_*"))
            if os.path.exists(os.path.join(d, "library.db")))
        if not dirs:
            sys.exit("no goldens dir with a library.db (run export_station_goldens.py)")
        gd = dirs[-1]
    path = os.path.join(gd, "acoustic.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(real(gd), f, ensure_ascii=False)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
