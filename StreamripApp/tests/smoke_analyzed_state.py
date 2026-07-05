"""Real-library smoke harness for the acoustic-graph + walk pipeline.

Not part of the unittest suite — invoked manually:

    python tests/smoke_analyzed_state.py [path-to-analysed.zip]

Defaults to the most recent .analysed.zip under ../../tools/analyzed_states.

What it does, in order:
  1. Extract the analysed state bundle to a scratch dir.
  2. Open the bundled library.db via DatabaseManager (read-write — we'll
     write fresh edges into a working copy, not the bundle).
  3. Rebuild the acoustic + metadata graph from the new feature encoding.
  4. Pick a deliberately varied set of seeds — different artists, BPM
     buckets, and Camelot rings — to simulate how a real user clicks
     "Play Similar" on different parts of the library.
  5. Run tg.walk from each seed and print the full chain, annotated with
     artist / title / BPM / Camelot key / per-edge affinity weight.

The eyeball test is whether each chain "makes sense" musically. We can't
assert that automatically; the point of this harness is to make the
qualitative pattern visible.
"""

from __future__ import annotations

import asyncio
import glob
import os
import random
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# utils.config is real here — we want the real APP_DIR for custom moods.
# But we still scope to a temp directory so the script never touches the
# user's persistent state.
from utils import config
config.APP_DIR = tempfile.mkdtemp(prefix="smoke_app_dir_")

from utils import track_graph as tg
from utils.harmonic import key_index_to_camelot
from utils.db_manager import DatabaseManager


def _resolve_bundle(arg: str | None) -> str:
    if arg:
        return arg
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = sorted(
        glob.glob(os.path.join(here, "..", "..", "tools", "analyzed_states",
                               "*.analysed.zip")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("No .analysed.zip found under tools/analyzed_states/")
    return candidates[0]


def _extract(zip_path: str) -> str:
    out = tempfile.mkdtemp(prefix="smoke_state_")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    return out


def _camelot_str(ki: int) -> str:
    cam = key_index_to_camelot(ki)
    if cam is None:
        return "?"
    hour, ring = cam
    return f"{hour}{ring}"


async def _pick_varied_seeds(db: DatabaseManager, rng: random.Random,
                             n_per_bucket: int = 2) -> list[dict]:
    """Spans different BPM buckets and ring (major/minor) to mimic a user
    clicking Play Similar on tracks all over their library."""
    conn = await db.get_connection()
    # Per-bucket SELECTs so we don't just sample the most-common tempo zone.
    buckets = [(60, 95), (95, 120), (120, 150), (150, 200)]
    seeds: list[dict] = []
    for lo, hi in buckets:
        sql = """
            SELECT t.path, t.title, ar.name AS artist, pc.bpm, pc.key_index
            FROM tracks t
            JOIN albums al ON al.id = t.album_id
            JOIN artists ar ON ar.id = al.artist_id
            JOIN play_counts pc ON pc.track_path = t.path
            WHERE pc.timbre IS NOT NULL
              AND pc.bpm >= ? AND pc.bpm < ?
            ORDER BY RANDOM()
            LIMIT ?
        """
        async with conn.execute(sql, (lo, hi, n_per_bucket)) as cur:
            rows = await cur.fetchall()
        for r in rows:
            seeds.append(dict(r))
    rng.shuffle(seeds)
    return seeds


async def _print_walk(db: DatabaseManager, seed: dict, length: int) -> None:
    seed_path = seed["path"]
    seed_cid = await db.get_track_cluster(seed_path)
    seed_label = (
        f"{seed['artist']} — {seed['title']}  "
        f"(BPM {seed['bpm']:.0f}, key {_camelot_str(seed['key_index'])}, Cluster {seed_cid})"
    )
    print(f"\n=== SEED: {seed_label}")
    # Mirror the Play Similar callers — the seed-anchored smooth walk.
    out = await tg.walk(
        db,
        seed_path,
        length=length,
        avoid={seed_path},
    )
    if not out:
        print("  (no walk produced — graph dry from this seed)")
        return

    # Annotate each step with metadata + affinity to the previous step.
    prev = seed_path
    for i, path in enumerate(out, 1):
        row = await db.get_track_full(path)
        # get_track_full doesn't return key_index; pull it separately.
        conn = await db.get_connection()
        async with conn.execute(
            "SELECT key_index, bpm, cluster_id FROM play_counts WHERE track_path = ?",
            (path,),
        ) as cur:
            pc = await cur.fetchone()
        ki = int(pc["key_index"]) if pc and pc["key_index"] is not None else -1
        bpm = float(pc["bpm"]) if pc and pc["bpm"] is not None else 0.0
        cid = int(pc["cluster_id"]) if pc and pc["cluster_id"] is not None else None

        # Edge weight from prev → path (acoustic, if present).
        async with conn.execute(
            """SELECT weight, edge_kind FROM track_neighbors
               WHERE track_path = ? AND neighbor_path = ?
               ORDER BY CASE edge_kind WHEN 'acoustic' THEN 0
                                       WHEN 'artist'   THEN 1
                                       WHEN 'album'    THEN 2 END
               LIMIT 1""",
            (prev, path),
        ) as cur:
            edge = await cur.fetchone()
        if edge:
            w_str = f"{float(edge['weight']):.3f} via {edge['edge_kind']}"
        else:
            w_str = "(no direct edge — restart hop)"

        title = (row.get("title") if row else "") or os.path.basename(path)
        artist = (row.get("artist") if row else "") or "?"
        print(
            f"  step {i:>2}: {artist} — {title}  "
            f"(BPM {bpm:.0f}, key {_camelot_str(ki)}, Cluster {cid}, {w_str})"
        )
        prev = path


async def main() -> None:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    z_score = True
    args = sys.argv[1:]
    if "--no-z-score" in args:
        z_score = False
        args.remove("--no-z-score")
    arg = args[0] if args else None
    bundle = _resolve_bundle(arg)
    print(f"Using bundle: {bundle} (z_score={z_score})")
    extract_dir = _extract(bundle)
    # Work on a copy so the bundle (and any user-facing state) is left alone.
    work_db = os.path.join(extract_dir, "work.db")
    shutil.copy2(os.path.join(extract_dir, "library.db"), work_db)

    db = DatabaseManager(work_db)
    rng = random.Random(0)

    rows_with_features = await db.get_tracks_with_features(tg.FEATURES_VERSION)
    print(f"Library: {len(rows_with_features)} tracks with current-version features")

    print("\n— Building metadata edges…")
    art_n, alb_n = await tg.build_metadata_edges(db)
    print(f"  artist edges: {art_n}  album edges: {alb_n}")

    print("\n— Building acoustic edges (Camelot + log-BPM + local scaling)…")
    aco_n = await tg.build_acoustic_edges(db, z_score=z_score)
    print(f"  acoustic edges: {aco_n}")

    # Inspect the affinity distribution so we can see whether the kernel
    # behaved reasonably across the real library.
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT MIN(weight), AVG(weight), MAX(weight) FROM track_neighbors WHERE edge_kind = 'acoustic'"
    ) as cur:
        row = await cur.fetchone()
    print(
        f"  affinity range: min={row[0]:.4f}  mean={row[1]:.4f}  max={row[2]:.4f}"
    )

    seeds = await _pick_varied_seeds(db, rng, n_per_bucket=2)
    print(f"\nPicked {len(seeds)} seeds spanning the BPM range.")

    for seed in seeds:
        await _print_walk(db, seed, length=10)

    # Close the aiosqlite connection on the loop that owns it, otherwise the
    # background worker thread races shutdown with the closing loop and prints
    # a cosmetic "Event loop is closed" traceback after main returns.
    if db._conn is not None:
        await db._conn.close()
        db._conn = None


if __name__ == "__main__":
    asyncio.run(main())
