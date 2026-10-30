#!/usr/bin/env python3
"""Blind evaluation of AI metadata research (utils/metadata_ai.py).

Takes artists whose genre AND country MusicBrainz already confirmed, hides that
answer from the prompt, and asks the research call to find it. Read-only: it
never writes to the library DB (the research cache included).

The bar to ship research as a one-tap suggestion is not "mostly right" — it is
that misses come back as UNKNOWN rather than WRONG. A wrong country silently
moves an artist across Auto-Play's regional fence; an unknown costs nothing.

Metrics:
    country  right / wrong / blank    exact ISO match against MusicBrainz
    genre    family-hit / miss / blank   a predicted tag shares a genre family
                                         (genre_taxonomy.genre_families) with MB
    grounded share of answers that came back with web sources

Usage (key from env, never from argv so it stays out of shell history):
    GEMINI_API_KEY=… python tools/eval_ai_metadata.py --n 20 --greek 10
    GEMINI_API_KEY=… python tools/eval_ai_metadata.py --db path/to/library.db --model gemini-3.5-flash
"""

import argparse
import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db_manager import DatabaseManager  # noqa: E402
from utils import metadata_ai  # noqa: E402
from utils.genre_taxonomy import genre_families  # noqa: E402


def _default_db() -> str:
    try:
        from utils.filepath_utils import get_app_dir
        return os.path.join(get_app_dir(), "library.db")
    except Exception:
        return os.path.expanduser("~/library.db")


async def _truth_rows(db) -> list[dict]:
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT e.artist_name, e.country, e.genres FROM artist_enrichment e "
        "JOIN artists a ON a.name = e.artist_name "
        "WHERE e.source = 'musicbrainz' AND e.status = 'ok' "
        "AND e.country IS NOT NULL AND e.country <> '' "
        "AND e.genres IS NOT NULL AND e.genres NOT IN ('', '[]')"
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for name, country, genres in rows:
        try:
            g = [x.get("name") if isinstance(x, dict) else str(x) for x in json.loads(genres)]
        except Exception:
            continue
        out.append({"artist": name, "country": country.upper(), "genres": [x for x in g if x]})
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=_default_db())
    ap.add_argument("--n", type=int, default=20, help="artists to test")
    ap.add_argument("--greek", type=int, default=10, help="of which country GR")
    ap.add_argument("--model", default="")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        key, cfg_model = metadata_ai.load_research_settings()
        args.model = args.model or cfg_model
    if not key:
        sys.exit("Set GEMINI_API_KEY (or a key in Settings → AI Assistant).")
    from utils.llm_engine import DEFAULT_GEMINI_MODEL
    model = args.model or DEFAULT_GEMINI_MODEL

    db = DatabaseManager(args.db)
    await db.initialize()
    truth = await _truth_rows(db)
    rng = random.Random(args.seed)
    gr = [t for t in truth if t["country"] == "GR"]
    other = [t for t in truth if t["country"] != "GR"]
    rng.shuffle(gr)
    rng.shuffle(other)
    sample = gr[:args.greek] + other[:max(0, args.n - min(args.greek, len(gr)))]
    print(f"{len(truth)} confirmed artists; testing {len(sample)} "
          f"({sum(t['country'] == 'GR' for t in sample)} Greek) on {model}\n")

    vocab = [v["name"] for v in await db.get_genre_vocabulary(limit=60)]
    tally = {"c_right": 0, "c_wrong": 0, "c_blank": 0,
             "g_hit": 0, "g_miss": 0, "g_blank": 0, "grounded": 0, "errors": 0}
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for i, t in enumerate(sample):
            if i:
                await asyncio.sleep(4)
            ev = await db.get_artist_research_evidence(t["artist"])
            # Blind: hide what MusicBrainz said.
            ev.update(known_country="", known_area="", known_genres=[])
            try:
                data = await metadata_ai._generate(
                    key, model, metadata_ai.build_prompt(ev, vocab), session=session)
            except Exception as exc:
                print(f"  ERROR {t['artist']}: {exc}")
                tally["errors"] += 1
                if isinstance(exc, metadata_ai.ResearchUnavailable):
                    break
                continue
            sources, _q = metadata_ai._grounding(data)
            res = metadata_ai.validate(
                metadata_ai.parse_answer(metadata_ai._response_text(data)), sources, vocab)
            tally["grounded"] += bool(sources)

            cc = res["country"]
            c = "blank" if not cc else ("right" if cc == t["country"] else "wrong")
            tally[f"c_{c}"] += 1
            if not res["genres"]:
                g = "blank"
            else:
                g = "hit" if genre_families(res["genres"]) & genre_families(t["genres"]) else "miss"
            tally[f"g_{g}"] += 1
            flag = "  <-- WRONG" if (c == "wrong" or g == "miss") else ""
            print(f"  {t['artist'][:28]:28}  country {cc or '-':2} vs {t['country']} [{c:5}]  "
                  f"genres {', '.join(res['genres']) or '-'} vs {', '.join(t['genres'][:3])} "
                  f"[{g}] conf={res['confidence']} src={len(sources)}{flag}")

    n = len(sample) - tally["errors"]
    print("\nSummary")
    print(f"  country  right {tally['c_right']}  wrong {tally['c_wrong']}  blank {tally['c_blank']}")
    print(f"  genre    hit {tally['g_hit']}  miss {tally['g_miss']}  blank {tally['g_blank']}")
    print(f"  grounded {tally['grounded']}/{n}   errors {tally['errors']}")
    print("\nShip bar: wrong countries ≈ 0 and genre misses well below blanks.")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
