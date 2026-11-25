#!/usr/bin/env python3
"""Golden values for phase 7a (PORT_MAP rule 3): Flet's artist-metadata
backend run on a synthetic library with canned provider responses —
`metadata_enrich` (MusicBrainz matching, `lookup_artist`, `enrich_library`,
`enrich_pending`), `metadata_qobuz`, `metadata_audit`, `metadata_ai`
(prompt, parsing, validation, corroboration, `research_artists`,
`accept_suggestion`) and the `db_manager` writes behind them
(`fix_and_normalize_track_genres`, merges, bulk tagging, vocabulary...).

    test/goldens/phase7a.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_phase7a_goldens.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import db_manager as dbm  # noqa: E402
from utils import metadata_ai as mai  # noqa: E402
from utils import metadata_audit as maud  # noqa: E402
from utils import metadata_enrich as me  # noqa: E402
from utils import metadata_qobuz as mq  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "phase7a.json")

# ── fixture ──────────────────────────────────────────────────────────────────

ARTISTS = [
    (1, "Haris Alexiou"), (2, "Χάρις Αλεξίου"), (3, "Moose"),
    (4, "Toquel, Moose"), (5, "Moose x Λευτέρης"), (6, "Γιάννης Σπανός"),
    (7, "Laiko Band"), (8, "Kasabian"), (9, "Slipknot"), (10, "Unknown Artist"),
    (11, "Divers Act"), (12, "The Weeknd"), (13, "Weeknd"), (14, "Beyonce"),
    (15, "Beyoncé"), (16, "Nomad"), (17, "Drifter"), (18, "Kanye West"),
    (19, "Light"), (20, "Toquel, Fly Lo"), (21, "Ghost Name"),
    (22, "Sigur Rós"), (23, "Mid Score"), (24, "No Tags Band"),
    (25, "Franky Rizardo"), (26, "Mad Clip, DJ Stephan"), (27, "Thin Artist"),
    (28, "Greek Pop"), (29, "Offline Guy"),
]

# (id, artist_id, title, year, genre)
ALBUMS = [
    (1, 1, "Best Of", "1990", "Laiko"), (2, 1, "Η Αγάπη", "1995", None),
    (3, 2, "Best Of", "1990", "Entechno"), (4, 2, "Δίπλα Σου", "2001", "Divers"),
    (5, 3, "Loose", "2019", "Hip-Hop"), (6, 4, "Τράπ", "2020", "Trap"),
    (7, 5, "Νύχτα", "2021", "Hip-Hop"), (8, 6, "Τραγούδια", "1980", None),
    (9, 7, "Bouzouki Nights", "1999", "Laiko, Greek"),
    (10, 8, "Velociraptor!", "2011", "Rock"), (11, 9, "Iowa", "2001", "Rock"),
    (12, 9, "Vol 3", "2004", "Metal, Rock"), (13, 9, "AHIGH", "2008", "Metal"),
    (14, 10, "Mixtape", "", "Various"), (15, 11, "Grime Tape", "2018", "Divers"),
    (16, 12, "After Hours", "2020", "R&B"), (17, 13, "Starboy", "2016", "Pop"),
    (18, 14, "Lemonade", "2016", "Pop"), (19, 15, "Renaissance", "2022", "Dance"),
    (20, 16, "Desert", "2015", "Electronic"), (21, 17, "Roads", "2012", "Folk"),
    (22, 18, "Yeezus", "2013", "Hip-Hop"), (23, 19, "Φως", "2019", "Rap"),
    (24, 20, "Collab", "2020", None), (25, 21, "Nothing", "2010", None),
    (26, 22, "Takk", "2005", "Post-Rock"), (27, 23, "Middle", "2000", "Rock"),
    (28, 24, "Plain", "2009", None), (29, 25, "House Party", "2017", "House"),
    (30, 26, "Banger", "2021", "Rap"), (31, 27, "Thin", "2011", None),
    (32, 28, "Καλοκαίρι", "2018", "Pop"), (33, 29, "Unplugged", "2014", "Folk"),
]

# (id, album_id, title)
TRACKS = [
    (1, 1, "Σαν Τρελό Φορτηγό"), (2, 1, "Μπιρ Αλλάχ"), (3, 2, "Η Αγάπη"),
    (4, 3, "Νάνι Νάνι"), (5, 4, "Δίπλα Σου"), (6, 5, "Loose Intro"),
    (7, 6, "Τράπ Γραμμή"), (8, 7, "Νύχτα"), (9, 8, "Ένα Τραγούδι"),
    (10, 8, "Δεύτερο"), (11, 9, "Opa"), (12, 10, "Fire"), (13, 11, "People = Shit"),
    (14, 12, "Duality"), (15, 13, "Unsainted"), (16, 13, "Nero Forte"),
    (17, 14, "Track 1"), (18, 15, "Grime Bar"), (19, 16, "Blinding Lights"),
    (20, 17, "Starboy"), (21, 18, "Formation"), (22, 19, "Break My Soul"),
    (23, 20, "Sand"), (24, 21, "Wander"), (25, 22, "Black Skinhead"),
    (26, 23, "Φως Μου"), (27, 24, "Collab Song"), (28, 25, "Nada"),
    (29, 26, "Hoppípolla"), (30, 27, "Middle Song"), (31, 28, "Plain Song"),
    (32, 29, "Party Track"), (33, 30, "Banger Track"), (34, 31, "Thin Song"),
    (35, 32, "Ήλιος"), (36, 33, "Acoustic Set"),
]

# (artist_name, mbid, country, area, genres_json, source, score, status, provenance_json, fetched_at, attempts)
ENRICHMENT = [
    ("Haris Alexiou", "mb-ha", None, "Greece", '[{"name": "entechno", "count": 5}]',
     "musicbrainz", 100, "ok", '{"country": null, "genres": "musicbrainz"}', 1000.0, 0),
    ("Χάρις Αλεξίου", None, "GR", None, '[]', "qobuz", 0, "lowconfidence",
     '{"country": "musicbrainz", "genres": null}', 1001.0, 0),
    ("Moose", "mb-moose", "US", "United States", '[{"name": "indie rock", "count": 2}]',
     "musicbrainz", 100, "ok", None, 1002.0, 0),
    ("Toquel, Moose", None, "GR", None, '[{"name": "trap", "count": 2}, {"name": "hip hop", "count": 1}]',
     "musicbrainz", 50, "lowconfidence", None, 1003.0, 0),
    ("Moose x Λευτέρης", None, None, None, '[{"name": "hip hop", "count": 1}, {"name": "greek hip hop", "count": 1}]',
     "musicbrainz", 50, "ok", None, 1004.0, 0),
    ("Γιάννης Σπανός", "mb-gs", "us", None, '[]', "musicbrainz", 90, "ok", None, 1005.0, 0),
    ("Laiko Band", None, "FR", None, '[{"name": "laiko", "count": 1}]', "musicbrainz", 80, "ok", None, 1006.0, 0),
    ("Kasabian", None, "GB", None, '[{"name": "alternative rock", "count": 1}, {"name": "indie", "count": 1}]',
     "manual", 100, "ok", '{"country": null, "genres": "manual"}', 1007.0, 0),
    ("Slipknot", "mb-sk", "US", None, '[{"name": "nu metal", "count": 9}, {"name": "metal", "count": 4}]',
     "musicbrainz", 100, "ok", None, 1008.0, 0),
    ("Divers Act", "mb-da", "GB", None, '[{"name": "grime", "count": 5}, {"name": "hip hop", "count": 3}]',
     "musicbrainz", 100, "ok", None, 1009.0, 0),
    ("Nomad", None, "US", None, '[]', "musicbrainz", 100, "ok", None, 1010.0, 0),
    ("Drifter", None, "US", None, '[]', "musicbrainz", 100, "ok", None, 1011.0, 0),
    ("Mid Score", None, None, None, None, "musicbrainz", None, "retry", None, 1012.0, 1),
    ("Franky Rizardo", None, None, None, None, "musicbrainz", None, "retry", None, 1013.0, 1),
    ("Mad Clip, DJ Stephan", None, None, None, None, "musicbrainz", None, "retry", None, 1014.0, 1),
    ("Thin Artist", None, None, None, None, "musicbrainz", None, "retry", None, 1015.0, 1),
    ("Greek Pop", None, None, None, None, "qobuz", None, "error", None, 1016.0, 3),
]

AI_RESEARCH = [
    ("Laiko Band", '{"country": "GR", "country_basis": "web", "status": "ok", "genres": ["laiko"]}', "pending"),
    ("Nomad", '{"country": "DE", "country_basis": "web", "status": "ok", "genres": ["ambient"]}', "pending"),
    ("Drifter", '{"country": "DE", "country_basis": "web", "status": "ok"}', "pending"),
    ("Ghost Name", '{"status": "unknown", "search_failed": true, "genres": []}', "dismissed"),
]
DISMISSED = [("Drifter", "ai", "US"), ("Laiko Band", "tags", "IT")]
REJECTIONS = [("beyonce", "beyoncé")]

# ── canned providers ─────────────────────────────────────────────────────────

MB = {
    "Haris Alexiou": [{"id": "mb-ha", "name": "Haris Alexiou", "score": 100, "country": "GR",
                       "tags": [{"name": "laiko", "count": 3}, {"name": "entechno", "count": 5}]}],
    "Kanye West": [{"name": "Kanye West Tribute Band", "score": 100},
                   {"id": "mb-ye", "name": "Ye", "score": 90, "aliases": [{"name": "Kanye West"}],
                    "area": {"name": "United States", "iso-3166-1-codes": ["US"]},
                    "genres": [{"name": "hip hop", "count": 10}, {"name": "rap", "count": 10}]}],
    "Light": [{"id": "mb-light", "name": "Light", "score": 100}],
    "Toquel, Fly Lo": [{"id": "mb-beyond", "name": "Something Else", "score": 50,
                        "tags": [{"name": "cantopop", "count": 3}]}],
    "Toquel": [{"id": "mb-t", "name": "Toquel", "score": 100, "country": "GR",
                "genres": [{"name": "trap", "count": 2}]}],
    "Fly Lo": [],
    "Ghost Name": [],
    "Sigur Rós": [{"id": "mb-sr", "name": "Sigur Ros", "score": 95, "area": {"name": "Iceland"},
                   "tags": [{"name": "post-rock", "count": "4"}, {"name": "ambient", "count": None}]}],
    "Mid Score": [{"id": "mb-ms", "name": "Mid Score", "score": 60,
                   "genres": [{"name": "rock", "count": 1}]}],
    "No Tags Band": [{"id": "mb-nt", "name": "No Tags Band", "score": 100, "country": "GB"}],
    "Thin Artist": [{"name": "A Karaoke Thin Artist Tribute", "score": 80},
                    {"id": "mb-other", "name": "Somebody", "score": 70, "begin-area": {"iso-3166-1-codes": ["SE"]}}],
    "Franky Rizardo": [],
    "Mad Clip, DJ Stephan": [],
    "Mad Clip": [], "DJ Stephan": [],
    "Unknown Artist": [],
    "Greek Pop": [],
    "Beyonce": [{"id": "mb-b", "name": "Beyoncé", "score": 100, "country": "US",
                 "genres": [{"name": "r&b", "count": 7}]}],
    "Beyoncé": [{"id": "mb-b", "name": "Beyoncé", "score": 100, "country": "US",
                 "genres": [{"name": "r&b", "count": 7}]}],
    "Weeknd": [{"id": "mb-w", "name": "The Weeknd", "score": 88, "country": "CA"}],
    "The Weeknd": [{"id": "mb-w", "name": "The Weeknd", "score": 100, "country": "CA"}],
}
MB_DETAILS = {
    "mb-light": {"genres": [], "area": {"name": "The Netherlands"}},
    "mb-nt": {"tags": [{"name": "indie", "count": 2}], "country": "GB"},
    "mb-w": {"genres": [{"name": "r&b", "count": 3}], "country": "CA"},
}
MB_FAIL = {"Offline Guy": -1, "Rate Limited": 503}

QOBUZ = {
    "Franky Rizardo": {"id": 101, "name": "Franky Rizardo", "albums": [
        {"genre": {"id": 64, "slug": "house", "path": [64, 70]}},
        {"genre": {"id": 64, "slug": "house", "path": [64, 70]}},
        {"genre": {"slug": "electronique", "path": [64]}},
        {"genre": {"id": 1, "slug": "divers", "path": [1]}},
        {"genre": {"id": 64, "slug": "house", "path": [64, 70]}}]},
    "Thin Artist": {"id": 102, "name": "Thin Artist", "albums": [
        {"genre": {"slug": "pop", "path": [112, 113]}}, {"genre": None}, {}]},
    "Greek Pop": {"id": 103, "name": "Greek Pop", "albums": [
        {"genre": {"slug": "grece", "path": [94, 200]}},
        {"genre": {"slug": "grece", "path": [94, 200]}},
        {"genre": {"slug": "", "id": 80}}]},
    "Mid Score": {"id": 104, "name": "Mid-Score", "albums": [
        {"genre": {"slug": "rap-hip-hop", "path": [133]}},
        {"genre": {"slug": "", "path": ["133"]}}]},
    "Kanye West": {"id": 105, "name": "Kanye West", "albums": []},
}
QOBUZ_ERROR = {"Qobuz Error"}

# What the Python side asked for, so the Dart tests can check the requests.
MB_REQUESTS: list = []
QOBUZ_REQUESTS: list = []


def _mb_key(path, params):
    if path == "artist":
        m = re.match(r'artist:"((?:[^"\\]|\\.)*)"', params["query"])
        return ("search", m.group(1).replace("\\", "") if m else "")
    return ("detail", path.split("/", 1)[1])


async def _fake_get(self, path, params, retries=3):
    MB_REQUESTS.append({"path": path, "params": dict(params)})
    kind, key = _mb_key(path, params)
    if kind == "search":
        if key in MB_FAIL:
            return None, MB_FAIL[key]
        return {"artists": [dict(a) for a in MB.get(key, [])]}, 200
    if key in MB_DETAILS:
        return dict(MB_DETAILS[key]), 200
    return None, 404


class FakeQobuz:
    async def search(self, media_type, query, limit=500):
        QOBUZ_REQUESTS.append(["search", media_type, query, limit])
        if query in QOBUZ_ERROR:
            raise RuntimeError("boom")
        hit = QOBUZ.get(query)
        items = [{"id": 999, "name": "Decoy Act"}]
        if hit:
            items.append({"id": hit["id"], "name": hit["name"]})
        return [{"artists": {"items": items}}]

    async def get_metadata(self, item_id, media_type):
        QOBUZ_REQUESTS.append(["get", item_id, media_type])
        for v in QOBUZ.values():
            if str(v["id"]) == item_id:
                return {"albums": {"items": v["albums"]}}
        return {}

    async def close(self):
        pass


async def _fake_open_client(*, raise_on_error=False):
    return FakeQobuz()


async def _fake_close_client(client):
    return None


me.MusicBrainzClient._get = _fake_get
mq.open_client = _fake_open_client
mq.close_client = _fake_close_client


# ── helpers ──────────────────────────────────────────────────────────────────

async def _new_db(path):
    db = dbm.DatabaseManager(path)
    await db.initialize()
    await db.close()
    db = dbm.DatabaseManager(path)
    await db.initialize()
    conn = await db.get_connection()
    for aid, name in ARTISTS:
        await conn.execute("INSERT INTO artists (id, name) VALUES (?, ?)", (aid, name))
    for alid, aid, title, year, genre in ALBUMS:
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre) VALUES (?, ?, ?, ?, ?)",
            (alid, aid, title, year, genre))
    for tid, alid, title in TRACKS:
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, path, duration, format, added_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tid, alid, title, f"/m/{tid}.flac", 100.0, "flac", 1000.0 + tid))
    for row in ENRICHMENT:
        await conn.execute(
            "INSERT INTO artist_enrichment (artist_name, mbid, country, area, genres, source, "
            "score, status, provenance, fetched_at, attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    for name, payload, state in AI_RESEARCH:
        await conn.execute(
            "INSERT INTO artist_ai_research (artist_name, payload, state, created_at) "
            "VALUES (?, ?, ?, 1000)", (name, payload, state))
    for row in DISMISSED:
        await conn.execute(
            "INSERT INTO metadata_audit_dismissed (artist_name, rule, value) VALUES (?, ?, ?)", row)
    for row in REJECTIONS:
        await conn.execute("INSERT INTO artist_alias_rejections (a, b) VALUES (?, ?)", row)
    await conn.commit()
    return db


async def _dump(db) -> dict:
    conn = await db.get_connection()
    out = {}
    for table, sql in (
        ("artists", "SELECT id, name, album_count, track_count FROM artists ORDER BY id"),
        ("albums", "SELECT id, artist_id, title, year, genre, genre_bucket, track_count "
                   "FROM albums ORDER BY id"),
        ("tracks", "SELECT id, album_id, artist_tag FROM tracks ORDER BY id"),
        ("enrichment", "SELECT artist_name, mbid, country, area, genres, source, score, status, "
                       "provenance, attempts FROM artist_enrichment ORDER BY artist_name"),
        ("aliases", "SELECT alias, canonical, enrichment FROM artist_aliases ORDER BY alias"),
        ("rejections", "SELECT a, b FROM artist_alias_rejections ORDER BY a, b"),
        ("research", "SELECT artist_name, payload, state FROM artist_ai_research ORDER BY artist_name"),
        ("dismissed", "SELECT artist_name, rule, value FROM metadata_audit_dismissed "
                      "ORDER BY artist_name, rule"),
    ):
        async with conn.execute(sql) as cur:
            out[table] = [list(r) for r in await cur.fetchall()]
    # Volatile: research payloads carry researched_at.
    for r in out["research"]:
        try:
            p = json.loads(r[1])
            p.pop("researched_at", None)
            r[1] = p
        except Exception:
            pass
    return out


def _strip_time(d):
    d = dict(d)
    d.pop("researched_at", None)
    return d


# ── pure-function cases ──────────────────────────────────────────────────────

NAME_PAIRS = [
    ("Cesária Evora", "Cesária Évora"), ("Kanye West", "Ye"), ("The Beatles", "Beatles"),
    ("Earth, Wind & Fire", "Earth Wind and Fire"), ("A B C", "A B"), ("Light", "Light Years"),
    ("Sigur Rós", "Sigur Ros"), ("", "x"), ("Haris Alexiou", "Alexiou Haris"),
    ("DJ Stephan", "Stephan"), ("Χάρις", "χαρις"),
]
LUCENE = ['AC/DC', 'Guns N\' Roses', 'P!nk', 'a+b (c) [d] {e} ^~*?:\\"', '']
GENRE_OBJS = [
    {"genres": [{"name": "rock", "count": 2}, {"name": "pop", "count": 5}, {"name": ""}, None]},
    {"genres": [], "tags": [{"name": "a", "count": "3"}, {"name": "b", "count": "x"}, {"name": "c", "count": 1.9}]},
    {"tags": [{"name": f"t{i}", "count": i % 3} for i in range(12)]},
    {},
]
COUNTRY_OBJS = [
    {"country": "gr "}, {"country": "GRC", "area": {"iso-3166-1-codes": ["DE"]}},
    {"area": {"iso-3166-1-codes": []}, "begin-area": {"iso-3166-1-codes": ["fr"]}},
    {"area": {"name": "Ukraine"}}, {"area": {"name": "Hong Kong SAR"}},
    {"begin-area": {"name": "Athens, Ελλάδα"}}, {"area": {"name": "Atlantis"}}, {},
]
QOBUZ_GENRES = [
    {"id": 64, "slug": "house", "path": [64, 70]}, {"slug": "rap-hip-hop", "path": [133]},
    {"slug": "divers", "path": [1]}, {"slug": "grece", "path": [94, 3]},
    {"slug": "musiques-du-monde", "path": [94]}, {"slug": "", "id": 80},
    {"slug": "", "path": ["112"]}, {"slug": "enfants", "path": [167]}, {"slug": "Deep-House"},
    {"path": [999]}, None, "x",
]
SKELETONS = [
    "Χάρις Αλεξίου", "Haris Alexiou", "The Weeknd", "Λευτέρης Πανταζής", "Lefteris Pantazis",
    "Οι Μπλε", "Mpampis Stokas", "Μπάμπης Στόκας", "Philippos", "Φίλιππος", "Beyoncé",
    "AAA bbb", "Ελευθερία Αρβανιτάκη", "Eleftheria Arvanitaki",
]
NORMS = ["Έλληνας τραγουδιστής!", "Ελληνίδα", "Café-Society", "  multiple   spaces  ", "ΣΟΦΟΣ"]
SNIPPETS = [
    {"title": "Haris Alexiou - Wikipedia", "uri": "https://en.wikipedia.org/wiki/Haris_Alexiou",
     "snippet": "Haris Alexiou is a Greek singer; her album Η Αγάπη was a hit."},
    {"title": "Alexiou hotel", "uri": "https://www.hotels.example/alexiou",
     "snippet": "Η Αγάπη hotel in Athens, Greece."},
    {"title": "Some American band", "uri": "https://music.example/x",
     "snippet": "An American rock band with a single called Best Of Times."},
]
EVIDENCE = {
    "artist": "Haris Alexiou",
    "albums": [{"title": "Η Αγάπη", "year": "1995"}, {"title": "Best Of", "year": ""}],
    "tracks": ["Σαν Τρελό Φορτηγό", "Intro"],
    "file_tags": ["Laiko"], "known_country": "", "known_area": "Greece",
    "known_genres": ["entechno"],
}
BASIS_CASES = [
    ("GR", SNIPPETS, EVIDENCE), ("US", SNIPPETS[2:], EVIDENCE), ("CY", [], EVIDENCE),
    ("DE", SNIPPETS[:1], EVIDENCE), ("", SNIPPETS, EVIDENCE),
    ("JP", [], {"artist": "x", "albums": [{"title": "ひらがな"}], "tracks": []}),
    ("ES", [{"title": "t", "snippet": "a Spanish-language Mexican singer", "uri": "u"}],
     {"artist": "x", "albums": [], "tracks": []}),
    ("ES", [{"title": "t", "snippet": "a spanish singer", "uri": "u"}],
     {"artist": "x", "albums": [], "tracks": []}),
    ("US", [{"title": "t", "snippet": "an African American usability expert", "uri": "u"}],
     {"artist": "x", "albums": [], "tracks": []}),
]
PARSE_CASES = [
    ('{"artists": [{"id": 2, "identified": true}, {"id": "1", "identified": false}]}', 2),
    ('```json\n[{"id": 1, "genres": ["a"]}, {"genres": ["b"]}]\n```', 2),
    ('Sure! {"identified": true, "genres": ["laiko"], "country": "GR"} hope it helps', 1),
    ('{"identified": true}', 2), ('no json here', 1), ('', 1),
    ('{"artists": [{"id": 7}, {"id": 1.9}, {"id": true}, "x", {"id": null}]}', 3),
    ('[1, 2', 1),
]
VALIDATE_CASES = [
    ({"identified": True, "genres": ["Laiko", "entechno", "Divers", "x" * 41, "pop", "rock"],
      "country": "el", "confidence": "HIGH", "summary": "  Greek singer  ", "matched_as": "Haris"},
     [], ["laiko"], True, None),
    ({"identified": True, "genres": "trap", "country": "US", "confidence": "medium"},
     [{"title": "t", "uri": "u"}], ["trap"], False, None),
    ({"identified": True, "genres": [], "country": "US", "confidence": "high"}, [], [], False, ""),
    ({"identified": True, "genres": ["Hip-Hop/Rap"], "country": "GR", "confidence": "low"},
     [], [], True, "contradicted"),
    ({"identified": False, "genres": ["x"], "country": "GR"}, [], [], True, "web"),
    ({"identified": True, "country": "ZZ", "confidence": 5}, [], [], True, "script"),
    ({}, [], [], True, None), (None, [], [], True, None),
]


def _canned_answer(n, mode):
    arts = []
    for i in range(1, n + 1):
        arts.append({"id": i, "identified": i != 2, "matched_as": f"Artist {i}",
                     "summary": f"Artist number {i}", "genres": ["Laiko", "pop"] if i % 2 else ["trap"],
                     "country": "GR" if i == 1 else "us", "confidence": "high"})
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({"artists": arts})}]}}]}


PROMPTS: list = []
GENERATE_CALLS: list = []


async def _fake_generate(api_key, model, prompt, *, session=None, timeout=90,
                         use_grounding=False, structured=False):
    PROMPTS.append(prompt)
    GENERATE_CALLS.append({"api_key": api_key, "model": model,
                           "use_grounding": use_grounding, "structured": structured})
    n = len(re.findall(r"^ARTIST \d+:", prompt, re.M))
    return _canned_answer(n, None)


SNIPPET_MAP = {
    "Haris Alexiou": SNIPPETS[:2],
    "Laiko Band": None,
    "Nomad": [],
}


async def _fake_snippets(artist, evidence=None, session=None, timeout=3.5):
    return SNIPPET_MAP.get(artist, [SNIPPETS[2]])


mai._generate = _fake_generate
mai._fetch_web_snippets = _fake_snippets
mai.load_research_settings = lambda: ("KEY", "gemini-test")
mai.load_research_mode = lambda: "hybrid"


async def _sleep0(*_a, **_k):
    return None


# ── build ────────────────────────────────────────────────────────────────────

TOOL_CASES = [
    ("apply_artist_metadata", {"artists": ["haris alexiou", "Kanye West", "Nobody Here", " "],
                               "genres": ["Laiko", " rebetiko ", "laiko", ""], "country": "gr"}),
    ("apply_artist_metadata", {"artist": "moose", "genres": ["Shoegaze"], "mode": "add"}),
    ("apply_artist_metadata", {"artist": "Light", "country": " us "}),
    ("apply_artist_metadata", {"artist": "Laiko Band"}),
    ("apply_artist_metadata", {"artist": "Drifter"}),
    ("apply_artist_metadata", {"artist": "Kasabian"}),
    ("apply_artist_metadata", {"artists": ["Nomad", "Kasabian"]}),
    ("apply_artist_metadata", {"artist": "Nomad", "genres": ["ambient"], "country": "XX"}),
    ("apply_artist_metadata", {"artists": ["Zzz", "O'Brien"], "genres": ["x"]}),
    ("apply_artist_metadata", {"genres": ["x"]}),
    ("apply_artist_metadata", {"artists": ["Slipknot"], "artist": "Kasabian",
                               "genres": ["metal"], "mode": "replace"}),
    ("research_artist_metadata", {"artist": "laiko band"}),
    ("research_artist_metadata", {"artist": "Kasabian", "refresh": True}),
    ("research_artist_metadata", {"artist": "Nobody Here"}),
    ("research_artist_metadata", {}),
]


async def _build() -> dict:
    tmp = tempfile.mkdtemp(prefix="mai_an_7a_")
    base_path = os.path.join(tmp, "base.db")
    db = await _new_db(base_path)
    await db.close()

    def copy(name):
        p = os.path.join(tmp, name)
        shutil.copy(base_path, p)
        return dbm.DatabaseManager(p)

    out: dict = {
        "fixture": {
            "artists": ARTISTS, "albums": ALBUMS, "tracks": TRACKS,
            "enrichment": ENRICHMENT, "ai_research": AI_RESEARCH,
            "dismissed": DISMISSED, "rejections": REJECTIONS,
        },
        "mb": MB, "mb_details": MB_DETAILS, "mb_fail": MB_FAIL, "qobuz": QOBUZ,
        "qobuz_error": sorted(QOBUZ_ERROR), "snippet_map": SNIPPET_MAP,
        "snippets": SNIPPETS, "evidence": EVIDENCE,
    }

    # Pure functions.
    out["name_close"] = [[a, b, me._name_close(a, b)] for a, b in NAME_PAIRS]
    out["lucene"] = [[s, me._escape_lucene(s)] for s in LUCENE]
    out["extract_genres"] = [[o, me._extract_genres(o)] for o in GENRE_OBJS]
    out["extract_country"] = [[o, me._extract_country(o)] for o in COUNTRY_OBJS]
    out["qobuz_tokens"] = [[g, mq._tokens_for_genre(g)] for g in QOBUZ_GENRES]
    out["skeleton"] = [[s, maud.skeleton(s)] for s in SKELETONS]
    out["ai_norm"] = [[s, mai._norm(s)] for s in NORMS]
    out["search_queries"] = [
        ["Haris Alexiou", EVIDENCE, mai._search_queries("Haris Alexiou", EVIDENCE)],
        ["X", {"albums": [{"title": "X"}, {"title": "Intro"}, {"title": "Go"}, {"title": "Real Album"}]},
         mai._search_queries("X", {"albums": [{"title": "X"}, {"title": "Intro"}, {"title": "Go"},
                                              {"title": "Real Album"}]})],
    ]
    out["on_target"] = mai.on_target_snippets(SNIPPETS, EVIDENCE)
    out["countries_named"] = [[s["title"] + " " + s["snippet"],
                               sorted(mai._countries_named(s["title"] + " " + s["snippet"]))]
                              for s in SNIPPETS]
    out["country_basis"] = [[c, sn, ev, mai.country_basis(c, sn, ev)] for c, sn, ev in BASIS_CASES]
    out["parse_answers"] = [[t, n, {str(k): v for k, v in mai.parse_answers(t, n).items()}]
                            for t, n in PARSE_CASES]
    out["validate"] = [[a, s, v, m, b, mai.validate(a, s, v, allow_memory_country=m, basis=b)]
                       for a, s, v, m, b in VALIDATE_CASES]
    out["clean_country"] = [[c, mai._clean_country(c)] for c in ["gr", " uk ", "EL", "XX", None, "", 7]]
    ev2 = dict(EVIDENCE, artist="Laiko Band", disputed="Tagged laiko, but the country is FR.")
    out["prompt"] = {
        "hybrid": mai.build_prompt([(EVIDENCE, SNIPPETS[:2]), (ev2, [])], ["laiko", "pop"], mode="hybrid"),
        "weights": mai.build_prompt([(EVIDENCE, [])], [], mode="weights"),
        "google": mai.build_prompt([(EVIDENCE, SNIPPETS)], ["x"] * 70, mode="google_grounding"),
    }
    out["finish"] = [
        _strip_time(mai._finish(EVIDENCE, {"identified": True, "genres": ["Laiko"], "country": "GR",
                                           "confidence": "medium"},
                                SNIPPETS[:2], False, ["laiko"], "hybrid", "m")),
        _strip_time(mai._finish(EVIDENCE, {"identified": True, "genres": [], "country": "US",
                                           "confidence": "high"}, [], True, [], "weights", "m")),
        _strip_time(mai._finish(EVIDENCE, None, [], False, [], "google_grounding", "m",
                                grounding=([{"title": "g", "uri": "https://g"}], ["q"]))),
    ]
    out["credit_text"] = {
        "summaries": [
            [s, me_summary] for s, me_summary in [
                ({"status": "error: Qobuz connection failed: x", "enriched": 0, "total": 3}, None),
                ({"status": "aborted", "enriched": 2, "total": 9}, None),
                ({"status": "cancelled", "enriched": 1, "total": 4}, None),
                ({"status": "completed", "enriched": 5, "total": 5}, None),
                ({}, None),
            ]
        ],
    }
    from utils.progress_notify import metadata_summary_text, metadata_retry_action
    out["summary_text"] = [[s, metadata_summary_text(s), metadata_retry_action(s)]
                           for s, _ in out.pop("credit_text")["summaries"]]

    # MusicBrainz lookups.
    MB_REQUESTS.clear()
    import aiohttp
    async with aiohttp.ClientSession() as session:
        client = me.MusicBrainzClient(session, contact="x@example.com")
        lookups = []
        for name, wg in [("Haris Alexiou", True), ("Kanye West", True), ("Light", True),
                         ("Light", False), ("Toquel, Fly Lo", True), ("Ghost Name", False),
                         ("Offline Guy", True), ("Rate Limited", True), ("Sigur Rós", True),
                         ("Mid Score", True), ("No Tags Band", True), ("No Tags Band", False),
                         ("Thin Artist", True), ("Mad Clip, DJ Stephan", True), ("", True),
                         ("haris alexiou", True)]:
            lookups.append([name, wg, await client.lookup_artist(name, with_genres=wg)])
        out["mb_lookups"] = lookups
        out["mb_lookup_requests"] = list(MB_REQUESTS)
        MB_REQUESTS.clear()
        out["mb_candidates"] = await client.search_candidates("Kanye West", limit=5)
        out["mb_candidates_requests"] = list(MB_REQUESTS)
    MB_REQUESTS.clear()

    # Qobuz genre lookups.
    fq = FakeQobuz()
    out["qobuz_lookups"] = [[n, await mq.lookup_artist_genres(fq, n)] for n in
                            ["Franky Rizardo", "Thin Artist", "Greek Pop", "Mid Score",
                             "Kanye West", "Mad Clip, DJ Stephan", "Qobuz Error", ""]]
    out["qobuz_lookup_requests"] = list(QOBUZ_REQUESTS)
    QOBUZ_REQUESTS.clear()

    # DB reads on the base fixture.
    db = dbm.DatabaseManager(base_path)
    await db.initialize()
    out["base"] = {
        "vocabulary": await db.get_genre_vocabulary(limit=60),
        "vocabulary_3": await db.get_genre_vocabulary(limit=3),
        "countries": await db.get_library_countries(),
        "low_confidence": await db.get_low_confidence_artists(),
        "gaps_everything": await db.get_metadata_gap_artists(limit=1000, everything=True),
        "workbench_items": {n: await db.get_artist_workbench_item(n)
                            for n in ["Kasabian", "Slipknot", "Ghost Name", "Nobody", "Moose"]},
        "evidence": {n: await db.get_artist_research_evidence(n)
                     for n in ["Haris Alexiou", "Ghost Name"]},
        "needing": await db.get_artists_needing_enrichment(with_attempts=True),
        "needing_failed": await db.get_artists_needing_enrichment(include_failed=True, with_attempts=True),
        "needing_incomplete": await db.get_artists_needing_enrichment(retry_incomplete=True, with_attempts=True),
        "needing_limit": await db.get_artists_needing_enrichment(limit=2, with_attempts=True),
        "researched": sorted(await db.get_researched_artist_names()),
        "audit_flags": await maud.audit_flags(db),
        "duplicates": await maud.duplicate_artists(db),
        "album_titles": await maud.album_titles(db, "Slipknot"),
        "aliases_none": await db.get_artist_aliases("Haris Alexiou"),
    }
    await db.close()

    # Album genre normalisation.
    db = copy("fix.db")
    await db.initialize()
    out["fix"] = {"summary": await db.fix_and_normalize_track_genres()}
    out["fix"]["after"] = (await _dump(db))["albums"]
    out["fix"]["second"] = await db.fix_and_normalize_track_genres()
    await db.close()

    # Writes.
    db = copy("writes.db")
    await db.initialize()
    writes = {}
    await db.upsert_artist_enrichment("Kasabian", country="US", genres=[{"name": "x", "count": 1}])
    await db.upsert_artist_enrichment("Ghost Name", mbid="m", country=None, genres=None,
                                      source="qobuz", score=5, status="lowconfidence",
                                      provenance={})
    writes["failure"] = [
        await db.record_enrichment_failure("Mid Score", source="qobuz", max_attempts=3),
        await db.record_enrichment_failure("Mid Score", source="musicbrainz", max_attempts=3),
        await db.record_enrichment_failure("Kasabian", source="musicbrainz"),
        await db.record_enrichment_failure("Slipknot", source="musicbrainz"),
        await db.record_enrichment_failure("Light", source="musicbrainz", max_attempts=1),
    ]
    await db.set_manual_artist_enrichment("Light", country="NL", genres=["dutch rock", " ", {"name": "x", "count": 3}])
    await db.set_manual_artist_enrichment("Nomad", country="", genres=[], origin="ai")
    writes["bulk_tag"] = await db.bulk_tag_artists(["Moose", "Sigur Rós", "Haris Alexiou"],
                                                   genre="Hip Hop, indie rock,, Trap", country=None)
    writes["bulk_tag_none"] = await db.bulk_tag_artists(["Moose"], genre=" ", country="")
    writes["set_genres"] = await db.set_artists_genres(["Slipknot", "Divers Act"],
                                                       ["Metal ", "metal", "NU METAL", ""], country="gb")
    writes["bulk_manual"] = await db.set_manual_artist_enrichment_bulk(["Drifter"], country="DE",
                                                                       genres=["folk"])
    writes["accept_matches"] = await db.accept_artist_matches(["Toquel, Moose", "Moose", ""])
    await db.confirm_artist_match("Ghost Name", mbid="mb-g", country="ES", genres=[])
    await db.reject_alias_pair("Weeknd", "The Weeknd")
    await db.dismiss_audit_flag("Moose", "collab", "US")
    await db.save_ai_research("Moose", {"status": "ok", "genres": ["trap"], "country": "GR",
                                        "researched_at": 1.5})
    await db.set_ai_research_state("Laiko Band", "accepted")
    await mai.accept_suggestion(db, "Moose")
    await mai.accept_suggestion(db, "Nomad", genres=["Ambient", "ambient", ""], country="")
    await mai.accept_suggestion(db, "Unknown Artist", genres=["lo-fi"])
    await mai.dismiss_suggestion(db, "Drifter")
    writes["state"] = await _dump(db)
    writes["vocabulary"] = await db.get_genre_vocabulary()
    await db.close()
    out["writes"] = writes

    # Merge and separate.
    db = copy("merge.db")
    await db.initialize()
    await db.merge_artists("Χάρις Αλεξίου", "Haris Alexiou")
    await db.merge_artists("Weeknd", "The Weeknd")
    merged = await _dump(db)
    merged["aliases_of"] = await db.get_artist_aliases("Haris Alexiou")
    errors = []
    for a, c in [("Haris Alexiou", "haris alexiou"), ("Nobody", "Moose"), ("", "Moose")]:
        try:
            await db.merge_artists(a, c)
            errors.append(None)
        except ValueError as e:
            errors.append(str(e))
    await db.separate_artist("Χάρις Αλεξίου")
    separated = await _dump(db)
    try:
        await db.separate_artist("Moose")
        sep_err = None
    except ValueError as e:
        sep_err = str(e)
    await db.close()
    out["merge"] = {"merged": merged, "errors": errors, "separated": separated,
                    "separate_error": sep_err}

    # enrich_library (manual Sync) against MusicBrainz, then Qobuz.
    for label, provider, kw in [
        ("mb_failed", "musicbrainz", {"include_failed": True}),
        ("mb_incomplete", "musicbrainz", {"retry_incomplete": True, "limit": 6}),
        ("qobuz", "qobuz", {"include_failed": True}),
    ]:
        db = copy(f"sync_{label}.db")
        await db.initialize()
        MB_REQUESTS.clear()
        QOBUZ_REQUESTS.clear()
        events = []
        summary = await me.enrich_library(
            db, provider=provider, progress=lambda i, t, n, r: events.append([i, t, n, r]), **kw)
        out[f"sync_{label}"] = {"kwargs": kw, "provider": provider, "summary": summary,
                                "events": events, "state": await _dump(db),
                                "mb_requests": list(MB_REQUESTS),
                                "qobuz_requests": list(QOBUZ_REQUESTS)}
        await db.close()

    # Abort on three consecutive errors (connection lost): nobody loses an attempt.
    saved = dict(MB_FAIL)
    for n in ["Haris Alexiou", "Kanye West", "Light", "Toquel, Fly Lo", "Toquel", "Fly Lo"]:
        MB_FAIL[n] = -1
    db = copy("sync_abort.db")
    await db.initialize()
    events = []
    summary = await me.enrich_library(db, provider="musicbrainz",
                                      progress=lambda i, t, n, r: events.append([i, t, n, r]))
    out["sync_abort"] = {"summary": summary, "events": events, "state": await _dump(db),
                         "mb_fail": dict(MB_FAIL)}
    await db.close()
    MB_FAIL.clear()
    MB_FAIL.update(saved)

    # enrich_pending (the automatic pass), both preferences.
    for pref in ["musicbrainz", "qobuz"]:
        db = copy(f"pending_{pref}.db")
        await db.initialize()
        events = []
        summary = await me.enrich_pending(db, preferred=pref,
                                          progress=lambda i, t, n, r: events.append([i, t, n, r]))
        out[f"pending_{pref}"] = {"summary": summary, "events": events, "state": await _dump(db)}
        await db.close()

    # Qobuz unusable: its artists roll to MusicBrainz.
    async def _broken_open(*, raise_on_error=False):
        raise RuntimeError("Qobuz client initialization failed (AuthenticationError: bad)")
    mq.open_client = _broken_open
    db = copy("pending_noqobuz.db")
    await db.initialize()
    events = []
    summary = await me.enrich_pending(db, preferred="qobuz",
                                      progress=lambda i, t, n, r: events.append([i, t, n, r]))
    out["pending_noqobuz"] = {"summary": summary, "events": events, "state": await _dump(db)}
    await db.close()
    mq.open_client = _fake_open_client

    # AI research flows.
    db = copy("research.db")
    await db.initialize()
    PROMPTS.clear()
    GENERATE_CALLS.clear()
    events = []
    real_sleep = asyncio.sleep
    asyncio.sleep = _sleep0
    try:
        summary = await mai.research_artists(
            db, ["Haris Alexiou", "Laiko Band", "Ghost Name", "Nomad", "Moose", "Kasabian"],
            progress=lambda i, t, n, r: events.append([i, t, n, _strip_time(r)]), batch_size=4)
        single = await mai.research_artist(db, "Slipknot", dispute="Tagged x, but the country is US.")
        cached = await mai.research_artist(db, "Haris Alexiou")
        forced = await mai.research_artist(db, "Haris Alexiou", force=True, mode="weights")
    finally:
        asyncio.sleep = real_sleep
    out["research"] = {
        "summary": summary, "events": events, "prompts": list(PROMPTS),
        "calls": list(GENERATE_CALLS), "single": _strip_time(single),
        "cached": _strip_time(cached), "forced": _strip_time(forced),
        "state": await _dump(db),
    }
    await db.close()
    # Jarvis's metadata write tools (phase 7b): each case on a fresh copy;
    # a confirmation is answered yes and the library dumped after.
    from utils import llm_tools as lt
    from utils.assistant_runner import AssistantRunner
    refreshes = []
    real_refresh = me.refresh_walk_models
    me.refresh_walk_models = lambda db, **kw: refreshes.append(1)
    asyncio.sleep = _sleep0
    tools = []
    try:
        for i, (tool, args) in enumerate(TOOL_CASES):
            db = copy(f"tool_{i}.db")
            await db.initialize()
            runner = AssistantRunner.__new__(AssistantRunner)
            runner.db = db
            runner._pending = None
            runner._agent_interrupt = None
            refreshes.clear()
            res = await lt._metadata_tool(tool, dict(args), runner)
            if isinstance(res, dict) and isinstance(res.get("summary"), str):
                res = dict(res)
            case = {"tool": tool, "args": args, "result": res, "interrupt": None,
                    "after": None, "refreshes": 0, "state": None}
            if runner._agent_interrupt is not None:
                ir = runner._agent_interrupt
                case["interrupt"] = {"spoken": ir.spoken, "displayed": ir.displayed}
                done = await runner._pending.on_yes_callback()
                case["after"] = {"spoken": done.spoken, "displayed": done.displayed}
                case["refreshes"] = len(refreshes)
                case["state"] = await _dump(db)
            tools.append(case)
            await db.close()
    finally:
        asyncio.sleep = real_sleep
        me.refresh_walk_models = real_refresh
    out["tools"] = tools
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def main():
    data = asyncio.run(_build())
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
