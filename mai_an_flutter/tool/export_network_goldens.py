#!/usr/bin/env python3
"""Golden values for the Acoustic Network view (PORT_MAP rule 3): what the
Flet app's Network view computes, so the Dart port can be checked.

The layout runs library.py's own code: `_build_interactive_network_canvas`
and `_apply_walk_seq` on a LibraryView built without a page (a stub page and
app, and a stub audio_engine for the playing path). The Flet controls it
builds are thrown away; the nodes, edges, projection and selection it leaves
on the view are the golden.

Two files:

  test/goldens/network_synthetic.json   (committed)
      A small library built in a fresh database (`SETUP`): clumps,
      coincident and collinear coordinates, tracks without coordinates,
      edges to missing tracks, acoustic rows beside the artist tier.
      `build_metadata_edges` at k=30 and k=3, `get_neighbors`,
      `get_tracks_pca_coords_for_paths`, `has_pca_coords`, and the view's
      load + layout + re-walk for fixed scenarios (walks given). No
      personal data.

  tools/state_backups/goldens/<bundle>/network.json   (gitignored)
      On a real library: `build_metadata_edges` (on a copy), and for fixed
      seeds at each density the neighbours, the walk (`track_graph.walk`),
      the layout, and a re-walk from a neighbour. Reads the library.db that
      export_station_goldens.py copied beside it (run that first).
          flutter test test/network_real_golden_test.dart

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_network_goldens.py            # newest goldens dir
    ~/miniconda3/bin/python tool/export_network_goldens.py --synthetic-only
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import random
import shutil
import sqlite3
import struct
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

import ui.views.library as L  # noqa: E402
from utils import track_graph as tg  # noqa: E402
from utils.db_manager import DatabaseManager  # noqa: E402

PHONE = (360, 640)    # page size → canvas 336 × 268
DESKTOP = (1200, 900)  # → 1176 × 378


def _blob(*xs: float) -> bytes:
    # 8 dims like a real (smaller) geometry; the view reads the first two.
    vals = list(xs) + [0.25] * (8 - len(xs))
    return struct.pack("<8f", *vals)


# ── The synthetic library ────────────────────────────────────────────────────
# (artist, album, genre, [(title, path, coords or None, play_count or None)])
# coords None: a play_counts row without coordinates; play_count None: no
# play_counts row at all.
_rng = random.Random(7)
LIBRARY = [
    ("Alpha", "A One", "Rock", [
        ("Origin", "/m/a/1.flac", (0.0, 0.0), 3),
        ("Twin", "/m/a/2.flac", (0.0, 0.0), 0),             # coincident
        ("Near", "/m/a/3.flac", (0.001, 0.0), 1),
        ("Nearer", "/m/a/4.flac", (0.001, 0.0005), 40),
        ("Off", "/m/a/5.flac", (0.3, 0.2), 0),
        ("Wide", "/m/a/6.flac", (-0.5, 0.4), 12),
    ]),
    ("Beta", "Clump", "Electronic, Pop", [
        (f"Clump {i}", f"/m/b/{i}.flac",
         (0.1 + _rng.uniform(-0.004, 0.004), -0.1 + _rng.uniform(-0.004, 0.004)),
         _rng.choice([0, 1, 2, 9, 100]))
        for i in range(1, 11)
    ]),
    ("Γάμμα", "Λαϊκά", "Laiko", [
        (f"Σειρά {i}", f"/m/c/{i}.flac", (0.2 + 0.002 * i, 0.2), i)
        for i in range(1, 6)
    ]),
    ("Delta", "Spread", None, [
        ("North", "/m/d/1.flac", (1.0, 1.0), 2),
        ("West", "/m/d/2.flac", (-1.2, 0.3), 0),
        ("South", "/m/d/3.flac", (0.0, -2.0), 5),
        (None, "/m/d/4.flac", (2.5, -0.4), 1),
    ]),
    ("Epsilon", "Partial", "Jazz", [
        ("Analysed", "/m/e/1.flac", (-0.2, -0.3), 4),
        ("No coords", "/m/e/2.flac", None, 2),
        ("No row", "/m/e/3.flac", None, None),
    ]),
    ("Zeta", "Far", "Shoegaze", [
        ("Outlier", "/m/f/1.flac", (5.0, 5.0), 0),
        ("Outlier 2", "/m/f/2.flac", (5.0, 4.9), 1),
    ]),
    ("Lonely", "Solo", "Ambient", [
        ("Alone", "/m/g/1.flac", (-3.0, 1.0), 0),
    ]),
]

# Rows beside the artist tier: acoustic edges as older databases have them
# (weights between 0 and 1, and over), one to a path not in the library and
# one to a track without coordinates.
EXTRA_EDGES = [
    ("/m/a/1.flac", "/m/d/1.flac", 0.93, "acoustic"),
    ("/m/a/1.flac", "/m/d/2.flac", 0.71, "acoustic"),
    ("/m/a/1.flac", "/m/gone.flac", 0.88, "acoustic"),
    ("/m/a/1.flac", "/m/e/2.flac", 0.4, "acoustic"),
    ("/m/a/1.flac", "/m/f/1.flac", 0.0, "acoustic"),
    ("/m/e/2.flac", "/m/e/1.flac", 0.5, "acoustic"),
]


def _setup(con: sqlite3.Connection) -> None:
    """Insert LIBRARY."""
    t = 1700000000
    for ai, (artist, album, genre, tracks) in enumerate(LIBRARY, start=1):
        con.execute("INSERT INTO artists (id, name) VALUES (?, ?)", (ai, artist))
        con.execute(
            "INSERT INTO albums (id, artist_id, title, genre) VALUES (?, ?, ?, ?)",
            (ai, ai, album, genre),
        )
        for n, (title, path, coords, pc) in enumerate(tracks, start=1):
            t += 1
            # Two artists share one added_date, so the edge order falls back
            # to the path.
            added = 1700000000 if artist == "Delta" else t
            con.execute(
                "INSERT INTO tracks (album_id, title, track_num, duration, path, "
                "format, added_date) VALUES (?, ?, ?, ?, ?, 'FLAC', ?)",
                (ai, title, n, 180.5 + n, path, added),
            )
            if pc is not None:
                con.execute(
                    "INSERT INTO play_counts (track_path, count, pca_coords) "
                    "VALUES (?, ?, ?)",
                    (path, pc, _blob(*coords) if coords else None),
                )
    con.commit()


def _edges(con) -> list[list]:
    return [list(r) for r in con.execute(
        "SELECT track_path, neighbor_path, weight, edge_kind FROM track_neighbors "
        "ORDER BY track_path, neighbor_path, edge_kind"
    )]


# ── The view, without a page ─────────────────────────────────────────────────

def _view(page: tuple[int, int], now_playing: str):
    L.audio_engine = SimpleNamespace(
        current_path=now_playing, is_playing=False, queue=[], current_index=0,
        is_shuffle=False,
    )
    v = L.LibraryView.__new__(L.LibraryView)
    v.app = SimpleNamespace(
        trigger_haptic=lambda *a, **k: None,
        autoplay=SimpleNamespace(buffer_paths=lambda: []),
    )
    v.page = SimpleNamespace(width=page[0], height=page[1],
                             run_task=lambda *a, **k: None)
    v.view_mode = "network"
    v._animated_list_wrapper = None
    # LibraryView.__init__'s network state.
    v._net_nodes = []
    v._net_node_by_path = {}
    v._net_edges = []
    v._net_walk_seq = []
    v._net_local_paths = set()
    v._net_walk_seed_path = None
    v._net_readout_walk_btn = None
    v._net_walk_task = None
    v._net_dims = (0, 0, 0)
    v._net_proj = None
    v._net_canvas_obj = None
    v._net_pressed = None
    v._net_pulse_overlay = None
    v._net_pulse_token = 0
    v._net_pulse_running = False
    v._net_readout = None
    v._net_readout_title = None
    v._net_readout_artist = None
    v._network_seed_path = None
    v._net_selected_path = None
    v._net_k_neighbors = 24
    v._net_walk_length = 10
    v._net_canvas = None
    v._net_track_panel = None
    v._net_split_column = None
    v._net_steps_chip_refresh = None
    v._net_list_view = None
    v._net_viewer = None
    v._net_graph_collapsed = False
    v._net_collapse_icon = None
    v._net_mode = "explore"
    v._net_mode_btn = None
    v._net_mode_icon = None
    v._net_mode_label = None
    v._net_reseed_task = None
    v._net_zoom = 1.0
    v._net_zoom_base = 1.0
    return v


def _state(v) -> dict:
    return {
        "dims": list(v._net_dims),
        "proj": list(v._net_proj) if v._net_proj else None,
        "nodes": [
            {k: nd[k] for k in ("path", "px", "py", "radius", "color", "is_seed",
                                "walk_step", "title", "artist", "album", "genre",
                                "play_count", "duration")}
            for nd in v._net_nodes
        ],
        "edges": [[e["src"], e["dst"], e["weight"], e["kind"]] for e in v._net_edges],
        "selected": v._net_selected_path,
        "walk_seq": list(v._net_walk_seq),
        "walk_seed": v._net_walk_seed_path,
        "local": sorted(v._net_local_paths),
    }


def _pca_rows(rows) -> list:
    return [[r["path"], (r["pca_coords"] or [])[:2], r["play_count"], r["title"],
             r["duration"], r["artist"], r["album"], r["genre"]] for r in rows]


def _nbrs(rows) -> list:
    return [[n["path"], n["weight"], n["edge_kind"], n["title"], n["artist"], n["album"]]
            for n in rows]


async def _any_pca_path(db) -> str:
    conn = await db.get_connection()
    async with conn.execute(
        """
        SELECT pc.track_path
        FROM play_counts pc
        INNER JOIN tracks t ON t.path = pc.track_path
        WHERE pc.pca_coords IS NOT NULL LIMIT 1
        """
    ) as cur:
        row = await cur.fetchone()
        return row[0] if row else ""


async def _load(db, *, seed, now_playing, selected, k, walk, page, walk_for=None):
    """library.py `load_library`'s network branch, with the walk given (or
    computed by `walk_for(seed)`) instead of `_compute_walk_seq`."""
    v = _view(page, now_playing)
    v._network_seed_path = seed
    v._net_selected_path = selected
    v._net_k_neighbors = k
    current_path = v._network_seed_path or now_playing or ""
    if not current_path:
        current_path = await _any_pca_path(db)
    neighbors_list, walk_paths, walk_seed = [], [], ""
    if current_path:
        neighbors_list = await tg.neighbors(db, current_path, k=k)
        walk_seed = v._net_selected_path or current_path
        walk_paths = list(walk) if walk_for is None else await walk_for(walk_seed)
    needed = [current_path] if current_path else []
    if walk_seed:
        needed.append(walk_seed)
    needed.extend(n["path"] for n in neighbors_list)
    needed.extend(walk_paths)
    pca_rows = await db.get_tracks_pca_coords_for_paths(needed)
    v._build_interactive_network_canvas(pca_rows, current_path, neighbors_list, walk_paths)
    return v, {
        "current_path": current_path,
        "walk_seed": walk_seed,
        "neighbors": _nbrs(neighbors_list),
        "walk": walk_paths,
        "needed": needed,
        "pca_rows": _pca_rows(pca_rows),
        "stats": f"{len(pca_rows)} TRACKS",
        "layout": _state(v),
    }


async def _rewalk(db, v, seed, seq) -> dict:
    """`_schedule_net_walk_refresh`'s body with the walk given."""
    missing = [p for p in seq if p not in v._net_node_by_path]
    new_rows = await db.get_tracks_pca_coords_for_paths(missing) if missing else []
    v._apply_walk_seq(seed, seq, new_rows)
    return {"seed": seed, "seq": seq, "missing": missing,
            "new_rows": _pca_rows(new_rows), "layout": _state(v)}


SCENARIOS = [
    # name, seed, now_playing, selected, k, walk, page
    ("acoustic rows, missing and uncoordinated neighbours", "/m/a/1.flac", "", None, 24,
     ["/m/b/3.flac", "/m/c/1.flac", "/m/d/2.flac", "/m/gone.flac", "/m/e/2.flac",
      "/m/f/1.flac"], PHONE),
    ("clump, playing node is the focal", "/m/b/1.flac", "/m/b/4.flac", "/m/b/2.flac", 12,
     ["/m/b/5.flac", "/m/c/2.flac", "/m/c/3.flac"], PHONE),
    ("density cap", "/m/b/1.flac", "", None, 3, ["/m/a/6.flac"], PHONE),
    ("selection without coordinates falls back", "/m/c/1.flac", "", "/m/e/3.flac", 16,
     [], DESKTOP),
    ("no seed: the first analysed track", None, "", None, 24, ["/m/a/5.flac"], PHONE),
    ("playing seed", None, "/m/c/3.flac", None, 24, ["/m/c/3.flac", "/m/g/1.flac"], PHONE),
    ("seed without coordinates", "/m/e/2.flac", "", None, 24, [], PHONE),
    ("single node", "/m/g/1.flac", "", None, 24, [], PHONE),
    ("nothing to draw", "/m/e/3.flac", "", None, 24, [], PHONE),
    ("far outliers", "/m/f/1.flac", "/m/a/1.flac", None, 36,
     ["/m/a/1.flac", "/m/d/3.flac", "/m/g/1.flac"], DESKTOP),
]

REWALKS = {
    0: [("/m/d/1.flac", ["/m/f/2.flac", "/m/d/3.flac", "/m/a/2.flac", "/m/gone.flac"]),
        ("/m/a/3.flac", ["/m/c/4.flac"]),
        ("/m/a/3.flac", [])],
    1: [("/m/b/7.flac", ["/m/b/1.flac", "/m/b/8.flac", "/m/a/1.flac", "/m/a/2.flac",
                         "/m/a/3.flac", "/m/a/4.flac"])],
}


def _hypot_cases() -> list:
    import math
    rng = random.Random(3)
    pairs = [(3.0, 4.0), (0.0, 0.0), (1e-300, 1e-300), (1e300, 1e300), (-2.5, 0.1),
             (0.1, 0.2), (1.0, 1e-17)]
    for _ in range(300):
        pairs.append((rng.uniform(-400, 400), rng.uniform(-400, 400)))
    for _ in range(100):
        pairs.append((rng.uniform(-1, 1) * 10 ** rng.randint(-8, 8),
                      rng.uniform(-1, 1) * 10 ** rng.randint(-8, 8)))
    return [[x, y, math.hypot(x, y)] for x, y in pairs]


async def _synthetic() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "library.db")
        db = DatabaseManager(path)
        await db.initialize()
        await db.close()
        # The second start adds play_counts.cluster_id (get_connection).
        db = DatabaseManager(path)
        await db.initialize()
        has_pca_empty = await db.has_pca_coords()
        con = sqlite3.connect(path)
        _setup(con)
        out: dict = {"library": [
            [artist, album, genre, [[t, p, list(c) if c else None, pc] for t, p, c, pc in tracks]]
            for artist, album, genre, tracks in LIBRARY
        ], "extra_edges": EXTRA_EDGES, "hypot": _hypot_cases()}
        try:
            out["has_pca_empty"] = has_pca_empty
            n3 = await tg.build_metadata_edges(db, k=3)
            con2 = sqlite3.connect(path)
            out["edges_k3"] = {"count": list(n3), "rows": _edges(con2)}
            n30 = await tg.build_metadata_edges(db)
            out["edges_k30"] = {"count": list(n30), "rows": _edges(con2)}
            con2.executemany(
                "INSERT OR REPLACE INTO track_neighbors VALUES (?, ?, ?, ?)", EXTRA_EDGES)
            con2.commit()
            # Rebuilding the artist tier leaves the other rows alone.
            await tg.build_metadata_edges(db)
            out["edges_final"] = _edges(con2)
            con2.close()
            out["has_pca"] = await db.has_pca_coords()
            out["neighbors"] = {
                f"{p}|{k}": _nbrs(await tg.neighbors(db, p, k=k))
                for p in ("/m/a/1.flac", "/m/b/1.flac", "/m/e/2.flac", "/m/g/1.flac",
                          "/m/gone.flac")
                for k in (3, 24)
            }
            out["pca_for_paths"] = [
                {"paths": ps, "rows": _pca_rows(await db.get_tracks_pca_coords_for_paths(ps))}
                for ps in (
                    [], ["/m/a/1.flac"], ["/m/e/1.flac", "/m/e/2.flac", "/m/e/3.flac"],
                    ["/m/d/4.flac", "/m/a/1.flac", "/m/a/1.flac", "/m/gone.flac", "/m/b/3.flac"],
                )
            ]
            out["scenarios"] = []
            for i, (name, seed, np_, sel, k, walk, page) in enumerate(SCENARIOS):
                v, doc = await _load(db, seed=seed, now_playing=np_, selected=sel,
                                     k=k, walk=walk, page=page)
                doc.update({"name": name, "seed": seed, "now_playing": np_,
                            "selected_in": sel, "k": k, "page": list(page)})
                doc["rewalks"] = [await _rewalk(db, v, s, q) for s, q in REWALKS.get(i, [])]
                out["scenarios"].append(doc)
        finally:
            con.close()
            await db.close()
    return out


# ── A real library ───────────────────────────────────────────────────────────

SEEDS = 8


async def _real(db_path: str) -> str:
    out_dir = os.path.dirname(db_path)
    with tempfile.TemporaryDirectory() as tmp:
        # build_metadata_edges writes: on a copy, never the golden library.db.
        copy = os.path.join(tmp, "library.db")
        shutil.copyfile(db_path, copy)
        db = DatabaseManager(copy)
        await db.initialize()
        try:
            n = await tg.build_metadata_edges(db)
            con = sqlite3.connect(copy)
            edges = [[a, b] for a, b, w, kind in _edges(con) if kind == "artist"]
            con.close()
        finally:
            await db.close()

    db = DatabaseManager(db_path)
    await db.initialize()
    try:
        con = sqlite3.connect(db_path)
        analysed = [r[0] for r in con.execute(
            "SELECT pc.track_path FROM play_counts pc JOIN tracks t ON t.path = pc.track_path "
            "WHERE pc.pca_coords IS NOT NULL ORDER BY pc.track_path")]
        con.close()
        seeds = random.Random(0).sample(analysed, min(SEEDS, len(analysed)))

        async def walk_for(seed):
            return list(await tg.walk(db, seed, length=10) or [])

        cases = []
        for seed in seeds:
            for k in (12, 24, 36):
                v, doc = await _load(db, seed=seed, now_playing="", selected=None, k=k,
                                     walk=None, page=PHONE, walk_for=walk_for)
                # The readout's walk button on the last neighbour, 15 steps.
                local = [p for p in doc["needed"][2:] if p in v._net_node_by_path]
                rewalks = []
                if local:
                    frm = local[-1]
                    seq = list(await tg.walk(db, frm, length=15) or [])
                    rewalks.append(await _rewalk(db, v, frm, seq))
                doc.update({"seed": seed, "k": k, "rewalks": rewalks})
                cases.append(doc)
        doc = {
            "db": os.path.basename(db_path),
            "metadata_edges": {"count": list(n), "rows": edges},
            "cases": cases,
        }
    finally:
        await db.close()
    out = os.path.join(out_dir, "network.json")
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic-only", action="store_true")
    args = ap.parse_args()

    syn = os.path.join(PORT, "test", "goldens", "network_synthetic.json")
    with open(syn, "w") as f:
        json.dump(asyncio.run(_synthetic()), f, ensure_ascii=False, indent=1)
    print("wrote", os.path.relpath(syn, PORT))
    if args.synthetic_only:
        return
    found = sorted(glob.glob(os.path.join(REPO, "tools", "state_backups", "goldens", "*",
                                          "library.db")))
    if not found:
        raise SystemExit("no goldens dir: run export_station_goldens.py first")
    print("wrote", os.path.relpath(asyncio.run(_real(found[-1])), PORT))


if __name__ == "__main__":
    main()
