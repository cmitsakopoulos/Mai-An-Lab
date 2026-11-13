"""Metadata audit — mistakes in metadata the library already has.

Enrichment fills blanks; nothing re-checks an answer once it's there. A short
name matched to the wrong MusicBrainz entity keeps its wrong country forever,
and the walk's country fence acts on it. This finds two kinds of problem, both
by plain rules (no model call, so it runs on every workbench load for free):

  • Contradictions. An artist whose country disagrees with their own titles'
    script (mostly Greek titles, country US), with their genre tags (tagged
    laiko, country FR), or with a web-backed AI answer.
  • Duplicates. One artist under two spellings — Greek and Latin script
    ('Χάρις Αλεξίου' / 'Haris Alexiou'), Greek case, or swapped word order.

Both surface under the workbench's Review filter. The model's part is the
research the user can run from there: a disputed artist's research prompt is
told what looks wrong (metadata_ai.research_artist(dispute=…)).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import defaultdict
from typing import Dict, List

from utils.genre_taxonomy import script_culture, tag_culture

logger = logging.getLogger(__name__)

# Countries whose artists write in Greek script.
_GREEK_COUNTRIES = {"GR", "CY"}
# Share of an artist's titles in Greek script that makes "not Greek" suspicious.
_SCRIPT_SHARE = 0.5
_MIN_TITLES = 2
# AI answers trusted enough to contradict the library (see metadata_ai.country_basis).
_TRUSTED_AI_BASIS = {"web", "grounding", "script"}

_PLACEHOLDER_ARTISTS = {"various artists", "unknown artist", "various", "va", "unknown"}


# ── contradictions ───────────────────────────────────────────────────────────

def _flag(rule: str, reason: str, suggest: str = "") -> dict:
    return {"rule": rule, "reason": reason, "suggest_country": suggest}


async def audit_flags(db) -> Dict[str, List[dict]]:
    """{artist_name: [flag, ...]} for artists whose country looks wrong.
    A flag is {'rule', 'reason' (one sentence for the row), 'suggest_country'}.
    The user's own answers (source 'manual') are never second-guessed, and a
    flag the user dismissed stays hidden while the country is unchanged."""
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT e.artist_name, UPPER(e.country), e.genres FROM artist_enrichment e "
        "JOIN artists a ON a.name = e.artist_name "
        "WHERE e.country IS NOT NULL AND e.country <> '' "
        "AND (e.source IS NULL OR e.source <> 'manual')"
    ) as cur:
        enriched = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    if not enriched:
        return {}

    titles: Dict[str, set] = defaultdict(set)
    file_genres: Dict[str, set] = defaultdict(set)
    async with conn.execute(
        "SELECT ar.name, al.title, al.genre, t.title FROM tracks t "
        "JOIN albums al ON al.id = t.album_id JOIN artists ar ON ar.id = al.artist_id "
        "JOIN artist_enrichment e ON e.artist_name = ar.name "
        "WHERE e.country IS NOT NULL AND e.country <> ''"
    ) as cur:
        for name, album, genre, track in await cur.fetchall():
            for t in (album, track):
                if t:
                    titles[name].add(t)
            if genre:
                file_genres[name].add(genre)

    ai: Dict[str, dict] = {}
    # A suggestion the user dismissed is one they disagreed with.
    async with conn.execute("SELECT artist_name, payload FROM artist_ai_research "
                            "WHERE state <> 'dismissed'") as cur:
        for name, payload in await cur.fetchall():
            try:
                ai[name.casefold()] = json.loads(payload)
            except Exception:
                continue

    collab = await _collaborator_cultures(conn)

    dismissed = await db.get_audit_dismissals()
    out: Dict[str, List[dict]] = {}
    for name, (country, genres_json) in enriched.items():
        flags: List[dict] = []
        ts = titles.get(name) or set()

        cc, n_linked, tags_linked = collab.get(name, (None, (0, 0), []))
        if cc and country not in _GREEK_COUNTRIES:
            n_gr, n_all = n_linked
            f = _flag("collab", f"{n_gr} of the {n_all} credits it shares here are Greek, but "
                               f"the country is {country}. Probably a different artist "
                               f"with the same name.", cc)
            f["suggest_genres"] = tags_linked
            flags.append(f)

        greek = sum(1 for t in ts if script_culture(t) == "GR")
        if (len(ts) >= _MIN_TITLES and greek / len(ts) >= _SCRIPT_SHARE
                and country not in _GREEK_COUNTRIES):
            flags.append(_flag("script", f"Most titles are in Greek, but the country is {country}.",
                               "GR"))

        tags = set()
        try:
            for g in json.loads(genres_json or "[]"):
                tags.add(g.get("name") if isinstance(g, dict) else str(g))
        except Exception:
            pass
        for raw in file_genres.get(name) or ():
            tags.update(p.strip() for p in re.split(r"[,;/|]", raw) if p.strip())
        cultural = sorted(t for t in tags if t and tag_culture(t) == "GR")
        if cultural and country not in _GREEK_COUNTRIES and not any(
                f["rule"] in ("script", "collab") for f in flags):
            flags.append(_flag("tags", f"Tagged {cultural[0]}, but the country is {country}.", "GR"))

        p = ai.get(name.casefold()) or {}
        ai_cc = (p.get("country") or "").upper()
        if (ai_cc and ai_cc != country and p.get("country_basis") in _TRUSTED_AI_BASIS
                and not (ai_cc in _GREEK_COUNTRIES and flags)):
            flags.append(_flag("ai", f"The web says {ai_cc}; the library says {country}.", ai_cc))

        flags = [f for f in flags
                 if dismissed.get((name.casefold(), f["rule"])) != country]
        if flags:
            out[name] = flags
    return out


# Greek collaborators needed (and a majority of all of them) to call a row a
# namesake. Mirrors the
# stations' rule (artist_graph._spread_culture), which already acts on it; this
# flag is how the user sees and settles it.
_MIN_COLLABS = 2


async def _collaborator_cultures(conn) -> Dict[str, tuple]:
    """{artist: ('GR', (n_greek, n_linked), their top genres)} for solo artists
    most of whose credits in the library are Greek (by country or script). A short
    Latin-script name ('Moose') matches a foreign namesake with full confidence;
    the acts it is credited with are the evidence that it isn't."""
    from utils.metadata_enrich import credit_keys
    async with conn.execute(
        "SELECT a.name, UPPER(COALESCE(e.country, '')), e.genres FROM artists a "
        "LEFT JOIN artist_enrichment e ON e.artist_name = a.name"
    ) as cur:
        rows = await cur.fetchall()
    keys = {name: credit_keys(name) for name, _, _ in rows}
    by_key: Dict[str, set] = defaultdict(set)
    for name, ks in keys.items():
        for k in ks:
            by_key[k].add(name)
    info = {name: (cc, g) for name, cc, g in rows}

    def greek(name):
        cc = info[name][0]
        return cc in _GREEK_COUNTRIES or script_culture(name) == "GR"

    out: Dict[str, tuple] = {}
    for name, ks in keys.items():
        if len(ks) != 1:
            continue
        linked = set().union(*(by_key[k] for k in ks)) - {name}
        n_gr = sum(1 for v in linked if greek(v))
        if n_gr < _MIN_COLLABS or n_gr <= len(linked) / 2:
            continue
        votes: Dict[str, int] = defaultdict(int)
        for v in linked:
            if not greek(v):   # 'Toquel, Light' → British prog rock: no evidence
                continue
            try:
                for g in json.loads(info[v][1] or "[]"):
                    nm = (g.get("name") if isinstance(g, dict) else str(g)) or ""
                    if nm and tag_culture(nm) != "GR":
                        votes[nm.lower()] += 1
            except Exception:
                continue
        top = [t for t, _ in sorted(votes.items(), key=lambda kv: -kv[1])[:3]]
        out[name] = ("GR", (n_gr, len(linked)), top)
    return out


# ── duplicates ───────────────────────────────────────────────────────────────

# Greek → Latin, digraphs first. Close to ELOT 743, then folded further below
# so the common romanisations of one name meet ('Lefteris'/'Leuteris').
_GREEK_DIGRAPHS = (
    ("ου", "u"), ("αι", "e"), ("ει", "i"), ("οι", "i"), ("υι", "i"),
    ("αυ", "av"), ("ευ", "ev"), ("μπ", "b"), ("ντ", "d"), ("γκ", "g"), ("γγ", "ng"),
    ("τσ", "ts"), ("τζ", "tz"),
)
_GREEK_LETTERS = dict(zip(
    "αβγδεζηθικλμνξοπρστυφχψωσ",
    ["a", "v", "g", "d", "e", "z", "i", "th", "i", "k", "l", "m", "n", "x", "o", "p",
     "r", "s", "t", "i", "f", "h", "ps", "o", "s"],
))
# Latin spellings of the same sounds, folded to one form.
_LATIN_FOLDS = (
    ("ou", "u"), ("ai", "e"), ("ei", "i"), ("oi", "i"), ("y", "i"),
    ("eu", "ev"), ("ef", "ev"), ("au", "av"), ("af", "av"),
    ("mp", "b"), ("nt", "d"), ("gk", "g"), ("gg", "ng"),
    ("ch", "h"), ("kh", "h"), ("ph", "f"), ("c", "k"), ("w", "o"),
)


def _plain(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").casefold()
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def skeleton(name: str) -> str:
    """A spelling-blind key: Greek transliterated, romanisation variants
    folded, word order ignored, doubled letters collapsed. Two names with the
    same skeleton are very likely one artist."""
    s = _plain(name)
    s = re.sub(r"^(the|οι|ο|η|τα)\s+", "", s)
    for g, l in _GREEK_DIGRAPHS:
        s = s.replace(g, l)
    s = "".join(_GREEK_LETTERS.get(ch, ch) for ch in s)
    words = []
    for w in re.findall(r"[a-z0-9]+", s):
        for a, b in _LATIN_FOLDS:
            w = w.replace(a, b)
        w = re.sub(r"(.)\1+", r"\1", w)
        if w:
            words.append(w)
    return " ".join(sorted(words))


async def duplicate_artists(db) -> List[dict]:
    """Pairs of artist rows that look like one artist:
    [{'alias', 'canonical', 'alias_tracks', 'canonical_tracks'}]. The name
    with more tracks is proposed as the one to keep. Pairs the user said are
    different never come back."""
    conn = await db.get_connection()
    async with conn.execute("SELECT name, track_count FROM artists") as cur:
        rows = [(r[0], r[1] or 0) for r in await cur.fetchall() if r[0]]
    rejected = await db.get_alias_rejections()
    groups: Dict[str, list] = defaultdict(list)
    for name, n in rows:
        if _plain(name).strip() in _PLACEHOLDER_ARTISTS:
            continue
        key = skeleton(name)
        if len(key.replace(" ", "")) >= 3:
            groups[key].append((name, n))
    out: List[dict] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (-m[1], m[0]))
        keep, keep_n = members[0]
        for name, n in members[1:]:
            if tuple(sorted((name.casefold(), keep.casefold()))) in rejected:
                continue
            out.append({"alias": name, "canonical": keep,
                        "alias_tracks": n, "canonical_tracks": keep_n})
    out.sort(key=lambda d: -(d["alias_tracks"] + d["canonical_tracks"]))
    return out


async def album_titles(db, artist_name: str, limit: int = 3) -> List[str]:
    """A few of an artist's album titles — the evidence shown next to a
    possible duplicate."""
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT al.title FROM albums al JOIN artists ar ON ar.id = al.artist_id "
        "WHERE ar.name = ? ORDER BY al.track_count DESC LIMIT ?", (artist_name, limit),
    ) as cur:
        return [r[0] for r in await cur.fetchall()]
