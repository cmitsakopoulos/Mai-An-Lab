#!/usr/bin/env python3
"""
walk_probe.py — eyeball the *quality* of the graph walk against a real analyzed
library image, and A/B the metadata contribution.

Why
---
The unit tests only prove the walk plumbing (metadata degrades gracefully, the
dispatcher routes on `algorithm`). They can't tell you whether the queues feel
better. This tool runs the actual walkers over a real DB image and prints the
resulting queues so you can read them, plus cheap coherence numbers as guard
rails. Your ear is the real judge.

It runs two walks from each seed, all same length, so you can see what the
metadata term buys you:

  • smooth+meta      the shipping walk    (metadata pool gate on)
  • smooth-acoustic  metadata OFF         (== the pure acoustic dual-similarity flow)

Readiness / build
-----------------
The metadata factor only fires if the library is enriched (artist_enrichment
country/genres) and, ideally, a genre_affinity model exists. And the walk needs
Zr geometry at all. This tool reports that readiness and, with --build, runs
the real pipeline (`build_acoustic_edges` + `build_metadata_edges`) into the DB:

  • --build            build the graph: Zr geometry + metadata edges + NPMI
                       genre model, from EXISTING enrichment.
  • --build --no-enrich  same thing (kept for compatibility).

NB --build does NOT enrich any more: MusicBrainz was decoupled out of
`build_acoustic_edges` so a 1 req/s network cap can't stall a graph rebuild.
An unenriched image walks on pure acoustics, and the metadata gate stays dark.
To enrich a throwaway image first, run `utils.metadata_enrich.enrich_library`
against it (StreamripApp/tools/enrich_artists.py wraps it).

Both --build modes MUTATE the --db file (Zr coords,
genre_affinity). Point it at a throwaway image, not your live app DB.

Usage
-----
    python tools/walk_probe.py --db tools/offload_cache/walk_diag_db/library.db
    python tools/walk_probe.py --db .../library.db --build --no-enrich
    python tools/walk_probe.py --db .../library.db --build            # full enrich
    python tools/walk_probe.py --db .../library.db --seed "Karras" --length 12
    python tools/walk_probe.py --db .../library.db --seeds 5 --rng 0
    # per-mega-genre eye test: 5 seeds from EACH bucket, shipping walk each:
    python tools/walk_probe.py --db .../library.db --by-genre --per-genre 5 --rng 0
    # auto-play station eye test (legs, genre hops, removal rebuild):
    python tools/walk_probe.py --db .../library.db --station --seeds 3
    python tools/walk_probe.py --db .../library.db --station --mode random --station-seed 83127
    python tools/walk_probe.py --db .../library.db --station --reject 2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
from collections import Counter
from itertools import combinations

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))

from utils.db_manager import DatabaseManager           # noqa: E402
from utils import track_graph as tg                    # noqa: E402

DEFAULT_DB = os.path.join(
    os.path.dirname(__file__), "offload_cache", "walk_diag_db", "library.db",
)


# ── readiness ─────────────────────────────────────────────────────────────────

async def readiness(db) -> dict:
    conn = await db.get_connection()

    async def one(sql):
        async with conn.execute(sql) as cur:
            return (await cur.fetchone())[0]

    r = {
        "tracks": await one("SELECT COUNT(*) FROM tracks"),
        "analyzed": await one("SELECT COUNT(*) FROM play_counts WHERE timbre IS NOT NULL"),
        # THE acoustic-readiness field: the walk loads persisted Zr coords.
        "coord_tracks": await one("SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL"),
        "acoustic_edges": await one("SELECT COUNT(*) FROM track_neighbors WHERE edge_kind='acoustic'"),
        "artist_edges": await one("SELECT COUNT(*) FROM track_neighbors WHERE edge_kind='artist'"),
        "clusters": await one("SELECT COUNT(DISTINCT cluster_id) FROM play_counts WHERE cluster_id IS NOT NULL"),
        "enriched_artists": await one("SELECT COUNT(*) FROM artist_enrichment"),
        "enriched_country": await one("SELECT COUNT(*) FROM artist_enrichment WHERE country IS NOT NULL AND country<>''"),
        "enriched_genres": await one("SELECT COUNT(*) FROM artist_enrichment WHERE genres IS NOT NULL AND genres<>''"),
    }
    # genre_affinity is stored as a single serialized row; report the real
    # number of NPMI pairs in the model, not the table row count.
    try:
        r["genre_affinity_pairs"] = len(await db.get_genre_affinity())
    except Exception:
        r["genre_affinity_pairs"] = 0
    r["tracks_with_enriched_artist"] = await one(
        """SELECT COUNT(*) FROM play_counts pc
           JOIN tracks t ON t.path=pc.track_path
           JOIN albums al ON al.id=t.album_id
           JOIN artists ar ON ar.id=al.artist_id
           JOIN artist_enrichment e ON e.artist_name=ar.name
           WHERE pc.timbre IS NOT NULL
             AND ((e.country IS NOT NULL AND e.country<>'') OR (e.genres IS NOT NULL AND e.genres<>''))"""
    )
    return r


def print_readiness(r: dict) -> None:
    print("── readiness ──────────────────────────────────────────────")
    print(f"  tracks                    : {r['tracks']}")
    print(f"  analyzed (timbre)         : {r['analyzed']}")
    print(f"  Zr coords (walk-ready)    : {r['coord_tracks']}")
    print(f"  acoustic edges (legacy)   : {r['acoustic_edges']}")
    print(f"  artist edges              : {r['artist_edges']}")
    print(f"  Louvain clusters          : {r['clusters']}")
    print(f"  enriched artists          : {r['enriched_artists']} "
          f"(country {r['enriched_country']}, genres {r['enriched_genres']})")
    print(f"  genre_affinity pairs      : {r['genre_affinity_pairs']}")
    cov = (100.0 * r["tracks_with_enriched_artist"] / r["analyzed"]) if r["analyzed"] else 0.0
    print(f"  analyzed w/ enriched artist: {r['tracks_with_enriched_artist']} ({cov:.0f}% metadata coverage)")
    if not r["coord_tracks"]:
        print("  ⚠ no Zr coordinates — walks will be empty. Run with --build.")
    if not r["genre_affinity_pairs"]:
        print("  ⚠ no genre_affinity model — genre term falls back to Dice overlap.")
    print()


# ── metadata lookups for display ──────────────────────────────────────────────

async def _titles(db, paths):
    if not paths:
        return {}
    conn = await db.get_connection()
    out = {}
    ph = ",".join("?" * len(paths))
    async with conn.execute(
        f"SELECT path, title FROM tracks WHERE path IN ({ph})", paths
    ) as cur:
        for row in await cur.fetchall():
            out[row["path"]] = row["title"]
    return out


async def _edge_weight(db, a, b):
    """Self-tuning acoustic affinity a↔b (the step-coherence proxy), or None if
    either track has no coordinates.

    Computed from the SAME coordinate graph the walk scores with —
    exp(-d²/(σ_a·σ_b)) over the persisted Zr coords — rather than read out of
    `track_neighbors`. The old edge-table lookup returned None for every pair
    once the geometry moved to coordinates, which silently pinned the reported
    coherence at 0.000 on every run and made the metric look like a walk
    regression instead of a dead lookup."""
    graph = await tg._coord_graph_cached(db)
    if graph is None:
        return None
    ia = graph["path_to_idx"].get(a)
    ib = graph["path_to_idx"].get(b)
    if ia is None or ib is None:
        return None
    U = graph["X_unit"]
    return float(U[ia] @ U[ib])


def _jaccard(a, b) -> float:
    if not a or not b:
        return 0.0
    a, b = set(a), set(b)
    u = a | b
    return len(a & b) / len(u) if u else 0.0


async def summarize(db, seed, queue):
    """Coherence + cohesion metrics for one queue (seed prepended)."""
    full = [seed] + queue
    meta = await db.get_artist_meta_for_paths(full)
    clusters = {p: await db.get_track_cluster(p) for p in full}

    # step-to-step acoustic affinity
    weights = []
    for a, b in zip(full, full[1:]):
        w = await _edge_weight(db, a, b)
        if w is not None:
            weights.append(w)
    coherence = sum(weights) / len(weights) if weights else 0.0

    # genre cohesion: mean pairwise Jaccard of genre token sets across the queue
    gsets = [meta.get(p, {}).get("genres") or frozenset() for p in full]
    pairs = list(combinations(gsets, 2))
    cohesion = (sum(_jaccard(a, b) for a, b in pairs) / len(pairs)) if pairs else 0.0

    # cluster switches
    seq = [clusters[p] for p in full]
    switches = sum(1 for a, b in zip(seq, seq[1:]) if a is not None and b is not None and a != b)

    artists = {meta.get(p, {}).get("artist") for p in queue}
    return {
        "coherence": coherence,
        "cohesion": cohesion,
        "switches": switches,
        "distinct_artists": len({a for a in artists if a}),
        "meta": meta,
        "clusters": clusters,
    }


def _bucket_of(genres) -> str:
    """The coarse mega-genre bucket(s) for a track (genre_bucket, e.g. 'Rock/Alt').
    Shown so the eye test can spot drift a mega-genre AUC would miss: a walk that
    stays inside 'Rock/Alt' can still slide nu-metal → 70s classic rock, and only
    the fine [genres] tail — not the bucket — reveals it."""
    from utils.pca_engine import genre_bucket
    if not genres:
        return "--"
    bs = sorted({genre_bucket(g) for g in genres})
    return "/".join(bs)[:10] if bs else "--"


async def print_walk(db, label, seed, queue, titles):
    s = await summarize(db, seed, queue)
    print(f"  [{label}]  coherence={s['coherence']:.3f}  "
          f"genre-cohesion={s['cohesion']:.3f}  cluster-switches={s['switches']}  "
          f"artists={s['distinct_artists']}  len={len(queue)}")
    prev = seed
    for i, p in enumerate(queue, 1):
        m = s["meta"].get(p, {})
        w = await _edge_weight(db, prev, p)
        wtxt = f"{w:.2f}" if w is not None else "  · "
        gset = m.get("genres") or frozenset()
        genres = ",".join(sorted(gset))[:30]
        bucket = _bucket_of(gset)
        country = m.get("country") or "--"
        cid = s["clusters"].get(p)
        cid = f"C{cid}" if cid is not None else "C·"
        title = (titles.get(p) or os.path.basename(p))[:30]
        artist = (m.get("artist") or "?")[:18]
        print(f"     {i:>2}. aff={wtxt} {cid:<4} {country:<3} {bucket:<10} "
              f"{title:<30} — {artist:<18} [{genres}]")
        prev = p
    print()


# ── seed selection ────────────────────────────────────────────────────────────

async def pick_seeds(db, n, rng, seed_query):
    """Seeds that are walkable (have Zr coordinates) AND have an enriched
    artist, so the A/B is meaningful. `seed_query` matches title or artist
    substring.

    Walkability is `pca_coords IS NOT NULL`, NOT a row in `track_neighbors`:
    the walk traverses the coordinate graph, and the acoustic edge tier stopped
    being written when the geometry moved to persisted coordinates — so joining
    it here silently returned zero seeds on every library, healthy or not."""
    conn = await db.get_connection()
    base = """
        SELECT DISTINCT pc.track_path AS path, t.title AS title, ar.name AS artist
        FROM play_counts pc
        JOIN tracks t   ON t.path = pc.track_path
        JOIN albums al  ON al.id = t.album_id
        JOIN artists ar ON ar.id = al.artist_id
        JOIN artist_enrichment e ON e.artist_name = ar.name
        WHERE pc.pca_coords IS NOT NULL
          AND ((e.country IS NOT NULL AND e.country<>'') OR (e.genres IS NOT NULL AND e.genres<>''))
    """
    params = []
    if seed_query:
        base += " AND (t.title LIKE ? OR ar.name LIKE ?)"
        params += [f"%{seed_query}%", f"%{seed_query}%"]
    async with conn.execute(base, params) as cur:
        rows = [(r["path"], r["title"], r["artist"]) for r in await cur.fetchall()]
    if not rows:
        return []
    rng.shuffle(rows)
    return rows[:n]


# Canonical print order for the mega-genre buckets (genre_bucket labels).
_BUCKET_ORDER = [
    "Hip-Hop", "Rock/Alt", "Metal", "Pop", "Electronic",
    "Soul/R&B", "Folk/Cntry", "Classical", "Other",
]


def _primary_bucket(genres) -> str:
    """A track's single mega-genre = the most common genre_bucket over its
    (multi-label) genre tokens, ignoring Unknown. This is how a track is
    assigned to one bucket for per-genre sampling."""
    from collections import Counter
    from utils.pca_engine import genre_bucket
    if not genres:
        return "Unknown"
    c = Counter(genre_bucket(g) for g in genres)
    c.pop("Unknown", None)
    return c.most_common(1)[0][0] if c else "Other"


async def pick_seeds_by_genre(db, per, rng):
    """Group every edged + genre-enriched track by its primary mega-genre and
    sample `per` random seeds from each. Returns {bucket: [(path,title,artist)]}."""
    conn = await db.get_connection()
    sql = """
        SELECT DISTINCT pc.track_path AS path, t.title AS title, ar.name AS artist
        FROM play_counts pc
        JOIN tracks t   ON t.path = pc.track_path
        JOIN albums al  ON al.id = t.album_id
        JOIN artists ar ON ar.id = al.artist_id
        JOIN artist_enrichment e ON e.artist_name = ar.name
        WHERE pc.pca_coords IS NOT NULL AND e.genres IS NOT NULL AND e.genres<>''
    """
    async with conn.execute(sql) as cur:
        rows = [(r["path"], r["title"], r["artist"]) for r in await cur.fetchall()]
    if not rows:
        return {}
    meta = await db.get_artist_meta_for_paths([r[0] for r in rows])
    groups: dict[str, list] = {}
    for path, title, artist in rows:
        genres = meta.get(path, {}).get("genres") or frozenset()
        bucket = _primary_bucket(genres)
        if bucket == "Unknown":  # genres present but parsed to no tokens — not a genre
            continue
        groups.setdefault(bucket, []).append((path, title, artist))
    out = {}
    for bucket, lst in groups.items():
        rng.shuffle(lst)
        out[bucket] = lst[:per]
    return out


# ── build ─────────────────────────────────────────────────────────────────────

async def build_graph(db, no_enrich: bool):
    # `no_enrich` is now a no-op: build_acoustic_edges never touches the network
    # (enrichment was decoupled out of it), so every build is an "existing
    # metadata only" build. The flag is kept so old command lines still run.
    print("── building graph (Zr geometry + edges, existing metadata only) ──")
    n_coords = await tg.build_acoustic_edges(db)
    n_art, n_alb = await tg.build_metadata_edges(db)
    # NB build_acoustic_edges returns the number of TRACKS given coordinates,
    # not an edge count — it persists geometry, not acoustic edge rows.
    print(f"  {n_coords} tracks projected; {n_art} artist, {n_alb} album edges\n")


# ── main ──────────────────────────────────────────────────────────────────────

async def run(args):
    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}")
        return 2
    print(f"DB: {args.db}\n")
    db = DatabaseManager(args.db)
    await db.initialize()
    try:
        return await _run_with_db(db, args)
    finally:
        # Close the aiosqlite worker thread before the loop tears down, else it
        # logs "Event loop is closed" on exit.
        await db.close()


async def _run_with_db(db, args):
    if args.length is None:
        args.length = 24 if args.station else 10
    r = await readiness(db)
    print_readiness(r)

    # Readiness is the persisted Zr geometry (what the walk actually loads),
    # NOT the acoustic edge table — build_acoustic_edges stopped writing edge
    # rows when the geometry moved to coordinates, so gating on edges made this
    # tool refuse to walk a perfectly healthy graph.
    if args.build or (r["coord_tracks"] == 0 and not args.no_build):
        await build_graph(db, args.no_enrich)
        r = await readiness(db)
        print_readiness(r)
    if not args.no_build and await tg.journey_graph_stale(db):
        # As the app does at startup (main._ensure_graph_built_async).
        print("── journey graph is from an older builder: rebuilding it ──\n")
        await tg.build_journey_graph(db)

    if r["coord_tracks"] == 0:
        print("No persisted Zr coordinates — nothing to walk. Re-run with --build.")
        return 1

    rng = random.Random(args.rng)

    if args.by_genre:
        return await _run_by_genre(db, args, rng)
    if args.station:
        return await _run_station(db, args, rng)

    seeds = await pick_seeds(db, args.seeds, rng, args.seed)
    if not seeds:
        print("No seeds matched (need acoustic neighbours + enriched artist).")
        return 1

    for path, title, artist in seeds:
        print("═" * 72)
        print(f"SEED: {title}  —  {artist}")
        print(f"      {path}")
        print("═" * 72)
        # A/B the metadata contribution: the shipping walk (metadata pool on)
        # vs the same walk with metadata off (== pure acoustic flow).
        smooth_meta = await tg.walk(db, path, length=args.length)
        smooth_aco = await tg.walk(db, path, length=args.length, veto_genre_floor=0.0)

        all_paths = list({path, *smooth_meta, *smooth_aco})
        titles = await _titles(db, all_paths)

        await print_walk(db, "smooth+meta   ", path, smooth_meta, titles)
        await print_walk(db, "smooth-acoustic", path, smooth_aco, titles)

        # Where does metadata change the smooth queue?
        if smooth_meta != smooth_aco:
            first_div = next(
                (i for i, (a, b) in enumerate(zip(smooth_meta, smooth_aco)) if a != b),
                min(len(smooth_meta), len(smooth_aco)),
            )
            print(f"  → metadata diverges from acoustic-only at step {first_div + 1}\n")
        else:
            print("  → metadata made NO difference to this queue "
                  "(seed/neighbours lack usable enrichment, or acoustic dominates)\n")
    return 0


async def _run_by_genre(db, args, rng):
    """Sample `--per-genre` seeds from each mega-genre bucket and print the
    shipping walk (smooth+meta) for each, so quality can be eye-tested one
    mega-genre at a time (e.g. read Hip-Hop separately from Rock/Alt)."""
    buckets = await pick_seeds_by_genre(db, args.per_genre, rng)
    if not buckets:
        print("No genre-enriched, edged seeds found to sample.")
        return 1
    ordered = ([b for b in _BUCKET_ORDER if b in buckets]
               + [b for b in buckets if b not in _BUCKET_ORDER])
    for bucket in ordered:
        seeds = buckets[bucket]
        print("\n" + "#" * 72)
        print(f"#  MEGA-GENRE: {bucket}   ({len(seeds)} seed{'s' if len(seeds) != 1 else ''})")
        print("#" * 72)
        for path, title, artist in seeds:
            walk = await tg.walk(db, path, length=args.length)  # shipping config
            titles = await _titles(db, list({path, *walk}))
            seed_meta = await db.get_artist_meta_for_paths([path])
            sgenres = seed_meta.get(path, {}).get("genres") or frozenset()
            print("─" * 72)
            print(f"SEED: {title} — {artist}   "
                  f"[{','.join(sorted(sgenres))[:40]}]")
            await print_walk(db, "smooth+meta", path, walk, titles)
    return 0


async def _run_station(db, args, rng):
    """Simulate auto-play stations exactly as the app opens them (same station
    shape as utils/autoplay.py) and print each track with its genre node, so
    legs and hops can be read. --reject K removes the first K recommendations
    of the first buffer, as a listener would, and prints the rebuilt buffer."""
    from utils import autoplay as ap_mod

    seeds = await pick_seeds(db, args.seeds, rng, args.seed)
    if not seeds:
        print("No seeds matched.")
        return 1
    payload = await db.get_journey_graph() or {}
    nodes = payload.get("nodes") or {}
    via = payload.get("via") or {}
    if not payload.get("adj"):
        print("NB no genre-adjacency graph: stations use the radius (no legs/hops).\n")
    length = args.length
    # The listener's learned hop penalties, as AutoPlay passes them.
    hop_weights = {(r["src"], r["dst"]): ap_mod.hop_weight(r)
                   for r in await db.get_hop_rejects()}
    blocked = [pair for pair, w in hop_weights.items() if w == 0]
    if blocked:
        print("Blocked hops: " + ", ".join(f"{a} → {b}" for a, b in blocked) + "\n")

    for path, title, artist in seeds:
        station_seed = args.station_seed
        if args.mode == "random" and station_seed is None:
            station_seed = rng.randrange(1 << 31)
        st_rng = random.Random(station_seed) if args.mode == "random" else None
        station = await tg.open_station(
            db, path, rng=st_rng, leg_len=ap_mod.LEG_LEN,
            max_per_artist=ap_mod.ARTIST_CAP, max_per_album=ap_mod.ALBUM_CAP,
            window=ap_mod.CAP_WINDOW, hop_weights=hop_weights,
        )
        print("═" * 72)
        print(f"STATION: {title} — {artist}   [{nodes.get(path, '?')}]   "
              f"{args.mode}" + (f", seed {station_seed}" if station_seed is not None else ""))
        print("═" * 72)
        if args.reject:
            first = await station.take(ap_mod.BUFFER_TARGET)
            # As AutoPlay._negative: an act rejected twice is banned first.
            removed = first[:args.reject]
            removed_rows = await db.get_tracks_brief(removed)
            per_act = Counter((removed_rows.get(p) or {}).get("artist") for p in removed)
            for p in removed:
                act = (removed_rows.get(p) or {}).get("artist")
                if act and per_act[act] >= ap_mod.ARTIST_NEGATIVES_TO_BAN:
                    station.ban_artist_of(p)
            station.end_leg(removed)
            rebuilt = await station.take(ap_mod.BUFFER_TARGET)
            await _print_station(db, "first buffer", first, nodes, via,
                                 removed=set(first[:args.reject]))
            await _print_station(db, f"after removing {args.reject}", rebuilt, nodes, via)
        else:
            await _print_station(db, f"{length} tracks", await station.take(length), nodes, via)
    return 0


async def _print_station(db, label, queue, nodes, via, removed=frozenset()):
    rows = await db.get_tracks_brief(queue)
    print(f"  [{label}]")
    prev = None
    for i, p in enumerate(queue, 1):
        node = nodes.get(p, "?")
        r = rows.get(p) or {}
        if prev is not None and node != prev:
            bridges = via.get(f"{prev}\t{node}") or []
            by = f" (via {r.get('artist')})" if r.get("artist") in bridges else ""
            print(f"     {'·' * 20} hop → {node}{by}")
        mark = " ✗" if p in removed else "  "
        print(f"   {mark}{i:>2}. {node:<14} {(r.get('title') or os.path.basename(p))[:32]:<32} "
              f"— {(r.get('artist') or '?')[:20]:<20} [{(r.get('album') or '')[:20]}]")
        prev = node
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--seeds", type=int, default=3, help="number of random seeds")
    ap.add_argument("--seed", default=None, help="title/artist substring to seed from")
    ap.add_argument("--length", type=int, default=None,
                    help="queue length (default 10; 24 with --station)")
    ap.add_argument("--rng", type=int, default=0, help="rng seed (reproducible)")
    ap.add_argument("--by-genre", action="store_true",
                    help="sample --per-genre seeds from EACH mega-genre and print "
                         "the shipping walk for each (per-genre eye test)")
    ap.add_argument("--per-genre", type=int, default=5,
                    help="seeds per mega-genre bucket in --by-genre mode (default 5)")
    ap.add_argument("--station", action="store_true",
                    help="simulate auto-play stations (legs, hops); --length defaults to 24")
    ap.add_argument("--mode", choices=("deterministic", "random"), default="deterministic",
                    help="station variety, as the app's Deterministic / Random pills")
    ap.add_argument("--station-seed", type=int, default=None,
                    help="with --mode random: replay a station by its logged seed")
    ap.add_argument("--reject", type=int, default=0,
                    help="with --station: remove the first K recommendations, show the rebuild")
    ap.add_argument("--build", action="store_true", help="(re)build the graph into --db")
    ap.add_argument("--no-enrich", action="store_true",
                    help="with --build: skip MusicBrainz, use existing enrichment")
    ap.add_argument("--no-build", action="store_true",
                    help="never auto-build even if there are no edges")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
