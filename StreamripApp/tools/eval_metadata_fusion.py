#!/usr/bin/env python3
"""Read-only evaluation of a low-weight metadata gate on the similarity walk.

Question (from the design discussion): if we fuse enriched metadata into the
walk's candidate scoring as a multiplicative gate

        eff = acoustic_affinity * (1 + lambda * S_meta(current, candidate))

does provenance/genre coherence rise WITHOUT trapping the walk on one artist?

This touches nothing in the live graph. It reads the persisted acoustic edges
(`track_neighbors`), the persisted Zr coords (`play_counts.pca_coords`) and the
`artist_enrichment` cache, then simulates greedy walks under several arms:

    baseline      lambda = 0                  (pure acoustic — control)
    genre         S = genre-token Jaccard
    country       S = same-country indicator
    genre+country S = 0.5*Jaccard + 0.5*country
    artist        S = same-artist indicator   (NEGATIVE control — should trap)

Greedy (argmax over unvisited candidates) is deliberate: same seeds, same graph,
only the scoring weight differs, so any metric change is attributable to the gate.

Metrics per walk, averaged over seeds:
    uniqA%   distinct artists / steps        (↑ good; collapse = trapping)
    maxRun   longest same-artist run         (↓ good; high = trapped)
    genreCoh consecutive steps sharing genre (↑ = more genre-coherent)
    cntryCoh consecutive steps same country  (↑ = more provenance-coherent)
    acL2     mean consecutive Zr L2 distance (continuity; must not blow up)

Usage:
    python tools/eval_metadata_fusion.py
    python tools/eval_metadata_fusion.py --length 16 --seeds 60 --lambdas 0.5,1.0
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db_manager import DatabaseManager  # noqa: E402


def _default_db() -> str:
    try:
        from utils.filepath_utils import get_app_dir
        return os.path.join(get_app_dir(), "library.db")
    except Exception:
        return os.path.expanduser("~/library.db")


def _canon(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


async def _load(db):
    """Return (adjacency, artist_of, zr_of, genre_tokens, country_of)."""
    conn = await db.get_connection()

    # Acoustic adjacency: path -> [(neighbor, weight), ...] desc by weight.
    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    async with conn.execute(
        "SELECT track_path, neighbor_path, weight FROM track_neighbors "
        "WHERE edge_kind = 'acoustic'"
    ) as cur:
        for src, dst, w in await cur.fetchall():
            adjacency[src].append((dst, float(w)))
    for src in adjacency:
        adjacency[src].sort(key=lambda t: -t[1])

    # path -> artist, path -> Zr vector (from the unified projection).
    artist_of: dict[str, str] = {}
    zr_of: dict[str, np.ndarray] = {}
    for row in await db.get_tracks_pca_coords():
        p = row["path"]
        artist_of[p] = row.get("artist") or ""
        coords = row.get("pca_coords")
        if coords:
            zr_of[p] = np.asarray(coords, dtype=np.float32)

    # artist -> genre-token set, artist -> country (from enrichment cache).
    genre_tokens: dict[str, set] = {}
    country_of: dict[str, str] = {}
    async with conn.execute(
        "SELECT artist_name, country, genres FROM artist_enrichment "
        "WHERE status IN ('ok', 'lowconfidence')"
    ) as cur:
        for name, country, genres in await cur.fetchall():
            if country:
                country_of[name] = country
            toks = set()
            try:
                for g in json.loads(genres or "[]"):
                    t = _canon(g.get("name", ""))
                    if t:
                        toks.add(t)
            except Exception:
                pass
            if toks:
                genre_tokens[name] = toks
    return adjacency, artist_of, zr_of, genre_tokens, country_of


def _build_npmi(genre_tokens):
    """Learn a genre×genre 'BLOSUM' from co-occurrence: how often two genre
    tokens co-tag the same artist, scored as NPMI ∈ [0,1] (self = 1). Returns a
    soft set-similarity gsim(A, B) that gives partial credit for *related* genres
    (soft-rock ↔ hard-rock), where flat Jaccard would score them 0."""
    import math
    from collections import defaultdict

    docs = [s for s in genre_tokens.values() if s]
    N = len(docs)
    df = defaultdict(int)
    co = defaultdict(int)
    for s in docs:
        toks = list(s)
        for a in toks:
            df[a] += 1
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                a, b = sorted((toks[i], toks[j]))
                co[(a, b)] += 1

    def npmi(a, b):
        if a == b:
            return 1.0
        cab = co.get((a, b) if a < b else (b, a), 0)
        if cab == 0 or N == 0:
            return 0.0
        pab = cab / N
        pmi = math.log(pab / ((df[a] / N) * (df[b] / N)))
        denom = -math.log(pab)
        return max(0.0, pmi / denom) if denom > 0 else 0.0

    def gsim(A, B):
        if not A or not B:
            return 0.0
        s1 = sum(max(npmi(a, b) for b in B) for a in A)
        s2 = sum(max(npmi(a, b) for a in A) for b in B)
        return (s1 + s2) / (len(A) + len(B))

    return gsim


def _jaccard(A, B):
    return len(A & B) / len(A | B) if (A or B) else 0.0


def _make_s_meta(artist_of, genre_tokens, country_of, mode, gsim_pmi):
    def s(a_path, b_path):
        aa, ab = artist_of.get(a_path, ""), artist_of.get(b_path, "")
        same_artist = bool(aa and aa == ab)
        if mode == "artist":
            return 1.0 if same_artist else 0.0
        ga, gb = genre_tokens.get(aa, set()), genre_tokens.get(ab, set())
        ca, cb = country_of.get(aa), country_of.get(ab)
        cty = 1.0 if (ca and ca == cb) else 0.0
        # Cross-artist only: genre tokens are artist-level, so within-artist
        # similarity is always maximal → it would trap (see results).
        jac_x = 0.0 if same_artist else _jaccard(ga, gb)
        pmi_x = 0.0 if same_artist else gsim_pmi(ga, gb)
        if mode == "genrex":
            return jac_x
        if mode == "genrexpmi":
            return pmi_x
        if mode == "country":
            return cty
        if mode == "countrygenrex":
            return 0.5 * cty + 0.5 * jac_x
        if mode == "countrygenrexpmi":
            return 0.5 * cty + 0.5 * pmi_x
        return 0.0
    return s


def _walk(seed, length, lam, s_meta, adjacency):
    path = [seed]
    visited = {seed}
    current = seed
    for _ in range(length):
        best, best_eff = None, -1.0
        for nbr, w in adjacency.get(current, ()):
            if nbr in visited:
                continue
            eff = w * (1.0 + lam * s_meta(current, nbr)) if lam > 0 else w
            if eff > best_eff:
                best, best_eff = nbr, eff
        if best is None:
            break
        path.append(best)
        visited.add(best)
        current = best
    return path


def _metrics(path, artist_of, zr_of, genre_tokens, country_of):
    n = len(path)
    artists = [artist_of.get(p, "") for p in path]
    uniq = len(set(a for a in artists if a)) / max(1, n)
    # longest same-artist run
    max_run = run = 1
    for i in range(1, n):
        run = run + 1 if artists[i] and artists[i] == artists[i - 1] else 1
        max_run = max(max_run, run)
    # consecutive coherence (only count pairs where both sides have the signal)
    g_hit = g_tot = c_hit = c_tot = 0
    l2s = []
    for i in range(1, n):
        a, b = artists[i - 1], artists[i]
        ga, gb = genre_tokens.get(a), genre_tokens.get(b)
        if ga and gb:
            g_tot += 1
            g_hit += 1 if (ga & gb) else 0
        ca, cb = country_of.get(a), country_of.get(b)
        if ca and cb:
            c_tot += 1
            c_hit += 1 if ca == cb else 0
        za, zb = zr_of.get(path[i - 1]), zr_of.get(path[i])
        if za is not None and zb is not None:
            l2s.append(float(np.linalg.norm(za - zb)))
    return {
        "len": n,
        "uniqA": uniq,
        "maxRun": max_run,
        "genreCoh": (g_hit / g_tot) if g_tot else float("nan"),
        "cntryCoh": (c_hit / c_tot) if c_tot else float("nan"),
        "acL2": (sum(l2s) / len(l2s)) if l2s else float("nan"),
    }


def _agg(rows):
    keys = ["len", "uniqA", "maxRun", "genreCoh", "cntryCoh", "acL2"]
    out = {}
    for k in keys:
        vals = [r[k] for r in rows if not (isinstance(r[k], float) and r[k] != r[k])]
        out[k] = (sum(vals) / len(vals)) if vals else float("nan")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=_default_db())
    ap.add_argument("--length", type=int, default=12)
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--lambdas", default="0.5,1.0",
                    help="comma-separated gate strengths to sweep")
    args = ap.parse_args()
    lambdas = [float(x) for x in args.lambdas.split(",") if x.strip()]

    if not os.path.exists(args.db):
        print(f"✗ no database at {args.db}")
        return 1

    db = DatabaseManager(args.db)
    adjacency, artist_of, zr_of, genre_tokens, country_of = await _load(db)

    enriched_artists = set(genre_tokens) | set(country_of)
    n_artists = len(set(a for a in artist_of.values() if a))
    print(f"DB: {args.db}")
    print(f"  acoustic edges: {sum(len(v) for v in adjacency.values())} | "
          f"tracks with Zr: {len(zr_of)} | "
          f"artists: {n_artists} | enriched: {len(enriched_artists)} "
          f"({100*len(enriched_artists)/max(1,n_artists):.0f}%)")

    # Seeds: tracks with edges AND an enriched artist (so the gate is active),
    # sampled deterministically for reproducibility across arms.
    candidates = sorted(
        p for p in adjacency
        if artist_of.get(p) in enriched_artists and p in zr_of
    )
    if not candidates:
        print("✗ no seeds with both acoustic edges and enriched metadata.")
        await db.close()
        return 1
    step = max(1, len(candidates) // args.seeds)
    seeds = candidates[::step][:args.seeds]
    print(f"  seeds: {len(seeds)} | walk length: {args.length}\n")

    gsim_pmi = _build_npmi(genre_tokens)
    arms = [("baseline", "genrex", 0.0)]
    for lam in lambdas:
        for mode in ("genrex", "genrexpmi", "country",
                     "countrygenrex", "countrygenrexpmi", "artist"):
            arms.append((f"{mode}", mode, lam))

    print(f"{'arm':16}{'λ':>5}  {'len':>4} {'uniqA%':>7} {'maxRun':>7} "
          f"{'genreCoh':>9} {'cntryCoh':>9} {'acL2':>7}")
    print("-" * 72)
    base = None
    for label, mode, lam in arms:
        s_meta = _make_s_meta(artist_of, genre_tokens, country_of, mode, gsim_pmi)
        rows = [
            _metrics(_walk(seed, args.length, lam, s_meta, adjacency),
                     artist_of, zr_of, genre_tokens, country_of)
            for seed in seeds
        ]
        a = _agg(rows)
        if base is None:
            base = a
        tag = "baseline" if lam == 0 else label
        print(f"{tag:16}{lam:>5.2f}  {a['len']:>4.1f} {100*a['uniqA']:>7.1f} "
              f"{a['maxRun']:>7.2f} {a['genreCoh']:>9.3f} {a['cntryCoh']:>9.3f} "
              f"{a['acL2']:>7.3f}")
        if lam == 0:
            print("-" * 72)

    print("\nRead: a good gate raises genreCoh/cntryCoh vs baseline while keeping")
    print("uniqA% high and maxRun low. The 'artist' arm should visibly trap")
    print("(uniqA% drops, maxRun climbs) — that's the negative control working.")
    await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
