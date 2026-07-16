#!/usr/bin/env python3
"""Deterministic A/B harness for track_graph.walk across refactor stages.

Runs the shipping walk (meta on) at temperature=0, mmr=0 so output is
reproducible, over a fixed diverse seed set (incl. the Greek-laiko boundary
case). Prints a compact, diffable queue per seed plus two guardrail numbers:
  • cohesion  = mean NPMI soft-set-sim of each queued track's genres vs the seed
  • foreign   = # queued tracks with soft-set-sim(seed) < 0.06 (would be vetoed)
"""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))

from utils.db_manager import DatabaseManager          # noqa: E402
from utils import track_graph as tg                    # noqa: E402
from utils.genre_similarity import soft_set_sim        # noqa: E402

SEEDS = [
    ("hiphop/US ", "/storage/emulated/0/Music/PHONE/02 - NEW DROP.m4a"),
    ("grime/GB  ", "/storage/emulated/0/Music/PHONE/04-Skepta-No Sleep.m4a"),
    ("metal/US  ", "/storage/emulated/0/Music/01. Black Label Society - Bleed for Me.m4a"),
    ("edm/GB    ", "/storage/emulated/0/Music/01. Blessings.flac"),
    ("laiko/GR  ", "/storage/emulated/0/Music/PHONE/01. Giorgos Mazonakis - Agapo Simeni.m4a"),
]
LEN = 10
FLOOR = 0.06


async def main(db_path):
    db = DatabaseManager(db_path)
    await db.initialize()
    genre_model = await db.get_genre_affinity()
    for label, seed in SEEDS:
        q = await tg.walk(db, seed, length=LEN, meta_lambda=0.35,
                          mmr_lambda=0.0, temperature=0.0)
        allp = [seed] + q
        meta = await db.get_artist_meta_for_paths(allp)
        sg = (meta.get(seed) or {}).get("genres") or frozenset()
        sims = []
        rows = []
        for p in q:
            m = meta.get(p) or {}
            g = m.get("genres") or frozenset()
            s = soft_set_sim(sg, g, genre_model) if (sg and g) else float("nan")
            sims.append(s)
            gs = ",".join(sorted(g))[:34]
            art = (m.get("artist") or "?")[:20]
            cty = (m.get("country") or "--")
            base = os.path.basename(p)[:40]
            mark = "  " if (s != s or s >= FLOOR) else "!!"  # !! = foreign
            rows.append(f"    {mark} sim={s:4.2f} {cty:3} {art:20} [{gs}]  {base}")
        valid = [s for s in sims if s == s]
        coh = sum(valid) / len(valid) if valid else float("nan")
        foreign = sum(1 for s in valid if s < FLOOR)
        seed_art = (meta.get(seed) or {}).get("artist", "?")
        print(f"\n=== {label} SEED {seed_art}  [{','.join(sorted(sg))[:40]}]")
        print(f"    cohesion={coh:4.2f}  foreign={foreign}/{len(valid)}  len={len(q)}")
        for r in rows:
            print(r)
    try:
        if db._conn is not None:
            await db._conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
