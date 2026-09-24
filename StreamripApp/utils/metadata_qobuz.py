"""Artist genre lookup via Qobuz — the store this library was bought from.

Why Qobuz, and why it is the ONLY supplement
--------------------------------------------
Measured against the nine artists that still had nothing after MusicBrainz and
the local file tags, Qobuz resolved SEVEN, and a full pass took the library's
unplaceable artists to zero. The house/techno corner is the clearest case:
`Franky Rizardo` (a Dutch house DJ with 122 albums on Qobuz), `Ku De Ta` and
`SELKER` all came back Dance/House.

A Deezer tier was built and then removed. It looked useful in isolation, but
once Qobuz was in front of it, it contributed ZERO across the whole 252-artist
library — because the file tags already carry whatever the download source
wrote (Deezer's own genre included, for a Deezer-sourced album), so the only
artists it could have helped were ones Qobuz also knows.

Three reasons it outperforms a general music API here:

  • It is the SOURCE. Most of this library was downloaded from Qobuz, so the
    artist string in `artists.name` is usually Qobuz's own spelling — name
    matching is near-exact rather than fuzzy.
  • Catalogue depth per artist. Qobuz returns 35-158 albums for these artists
    versus a handful from Deezer, so a genre consensus across albums is real
    evidence rather than one record's label.
  • The taxonomy is machine-readable. Each album's genre carries a stable
    integer `id`, a language-neutral `slug`, and a `path` of ancestor ids.
    That means we map 13 stable family IDs instead of chasing localised display
    names ("Alternatif et Indé", "Musiques du monde", "Électronique"), which is
    what a display-name-only API forces on you.

Design notes
------------
  • Reuses the app's existing `QobuzClient`, so there is no second credential
    path to keep working. It logs in exactly as search does — and note that a
    401 degrades to Qobuz's guest signed-catalogue mode, which still serves
    `artist/get` and `search`. Enrichment therefore keeps working even when the
    user's subscription token has gone stale.
  • Genres come from the artist's ALBUMS (Qobuz has no artist-level genre), and
    are weighted by how many albums attest them, so an artist's stable identity
    outranks a one-off.
  • Everything is folded onto the corpus vocabulary before it is stored: the NPMI model in `genre_similarity` learns from co-occurrence
    across THIS library's tags, so an unmapped spelling lands as a singleton
    with no learned relation to anything.
"""

from __future__ import annotations

import logging

from utils.metadata_enrich import _name_close

logger = logging.getLogger(__name__)

# Qobuz's 13 top-level genres (GET genre/list), by stable id. `path[0]` on an
# album's genre names one of these, so the family survives any renaming or
# localisation of the display string.
_FAMILY_BY_ID = {
    112: "rock",          # Pop/Rock — only used when no subgenre is available
    80:  "jazz",
    10:  "classical",
    6:   "chanson",
    64:  "electronic",
    127: "soul",
    116: "metal",
    133: "hip hop",
    2:   "folk",          # Blues/Country/Folk
    91:  "soundtrack",
    94:  "world",
    123: "reggae",
}

# Families that carry no genre information. 1 = "Divers", Qobuz's placeholder
# bucket, which the local tag cleaner already knows to drop; 167 = "Enfants".
_JUNK_FAMILY_IDS = {1, 167}

# "Musiques du monde" is organised GEOGRAPHICALLY, so most of its subgenre slugs
# are country names — a Greek pop singer with a German release came back tagged
# 'grece' and 'allemagne'. Those are provenance, not genre: `country` is a
# separate field the walk reads differently, and a country name in the genre set
# would sit in the NPMI corpus pretending to be a musical style. Under this
# family we therefore keep ONLY the slugs that name an actual style (mapped
# below) and collapse everything else to 'world'.
_GEOGRAPHIC_FAMILY_ID = 94

# Subgenre slugs whose literal form is either localised or absent from this
# library's vocabulary. Anything not listed passes through with hyphens turned
# into spaces, which is already correct for 'house', 'dance', 'pop', 'rock',
# 'metal', 'jazz', 'reggae', 'techno', 'soul' and most others.
# Derived by sweeping the 45 largest artists in the real library and collecting
# every slug Qobuz actually returned, rather than guessing from the taxonomy.
_SLUG_TO_CORPUS = {
    "rap-hip-hop": "hip hop",
    "alternatif-et-inde": "alternative rock",
    "punk-new-wave": "new wave",
    "musiques-du-monde": "world",
    "electronique": "electronic",
    "electro": "electronic",
    "electronique-ou-concrete": "electronic",
    "classique": "classical",
    "chanson-francaise": "chanson",
    "bandes-originales": "soundtrack",
    "bandes-originales-de-films": "soundtrack",
    "series-tv": "soundtrack",
    "soul-funk-r-b": "soul",
    "rb": "rhythm and blues",
    "blues-country-folk": "folk",
    "pop-rock": "pop rock",
    "afrique": "african",
    "amerique-latine": "latin",
    "divers": None,
    "enfants": None,
}

# How many of an artist's albums to read for a genre consensus. Qobuz returns
# well over a hundred for prolific artists; the identity is settled long before
# that and each extra page costs a request.
_ALBUM_SAMPLE = 12

# An artist needs this many albums carrying a usable genre before we believe
# the consensus. One album is a credit, not an identity.
_MIN_ATTESTING_ALBUMS = 2


def _tokens_for_genre(genre: dict) -> list[str]:
    """One Qobuz album-genre object → corpus tokens (specific first).

    Emits the subgenre when there is one, and falls back to the top-level family
    otherwise. It does NOT emit both for every album: 'Pop/Rock' would then
    stamp 'rock' onto every pop record the artist ever made."""
    if not isinstance(genre, dict):
        return []
    path = genre.get("path") or []
    family_id = path[0] if path else genre.get("id")
    try:
        family_id = int(family_id)
    except (TypeError, ValueError):
        family_id = None
    if family_id in _JUNK_FAMILY_IDS:
        return []

    slug = (genre.get("slug") or "").strip().lower()
    if slug in _SLUG_TO_CORPUS:
        mapped = _SLUG_TO_CORPUS[slug]
        return [mapped] if mapped else []
    if family_id == _GEOGRAPHIC_FAMILY_ID:
        # An unmapped slug here is a country, not a style (see above).
        return ["world"]
    if slug:
        # A leaf slug that needs no translation ('house', 'techno', 'pop').
        return [slug.replace("-", " ")]

    fam = _FAMILY_BY_ID.get(family_id)
    return [fam] if fam else []


async def lookup_artist_genres(client, name: str) -> dict:
    """Resolve one artist name → genres from Qobuz.

    `client` is a logged-in `QobuzClient` (the caller owns its lifecycle, so one
    enrichment pass shares a single login).

    Returns {'genres': [{'name', 'count'}], 'qobuz_id', 'matched_name'} on a
    confident hit, else {'genres': [], 'reason': <why>}. Never raises — this is a
    supplement, and a failure here must not take down a pass MusicBrainz has
    already partly filled.

    The name gate is the same folded comparison MusicBrainz matching uses, so an
    accent cannot split a name. Multi-artist credit strings correctly find
    nothing ('Mad Clip, DJ Stephan' has no Qobuz artist page), which is the
    right answer rather than a fuzzy guess at one of the members.
    """
    raw = (name or "").strip()
    if not raw or client is None:
        return {"genres": [], "reason": "unavailable"}

    try:
        pages = await client.search("artist", raw, limit=5)
    except Exception as exc:
        logger.warning("Qobuz artist search failed for %r: %s: %s",
                       raw, type(exc).__name__, exc)
        return {"genres": [], "reason": "offline"}

    items = []
    for page in pages or []:
        items += ((page.get("artists") or {}).get("items") or [])
    match = next((a for a in items if _name_close(raw, a.get("name") or "")), None)
    if match is None:
        return {"genres": [], "reason": "no_confident_match"}

    aid = match.get("id")
    try:
        detail = await client.get_metadata(str(aid), "artist")
    except Exception as exc:
        logger.warning("Qobuz artist/get failed for %r (%s): %s", raw, aid, exc)
        return {"genres": [], "reason": "offline"}

    albums = ((detail.get("albums") or {}).get("items") or [])[:_ALBUM_SAMPLE]
    counts: dict[str, int] = {}
    attesting = 0
    for al in albums:
        toks = _tokens_for_genre(al.get("genre") or {})
        if toks:
            attesting += 1
        for t in toks:
            counts[t] = counts.get(t, 0) + 1

    if not counts:
        return {"genres": [], "reason": "no_album_genres",
                "qobuz_id": aid, "matched_name": match.get("name")}
    if attesting < _MIN_ATTESTING_ALBUMS and len(albums) >= _MIN_ATTESTING_ALBUMS:
        # The catalogue is there but almost nothing is tagged — too thin to
        # assert an identity from.
        return {"genres": [], "reason": "thin_evidence",
                "qobuz_id": aid, "matched_name": match.get("name")}

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "genres": [{"name": n, "count": c} for n, c in ordered[:6]],
        "qobuz_id": aid,
        "matched_name": match.get("name"),
        "albums_seen": len(albums),
        "reason": None,
    }


async def open_client():
    """A logged-in `QobuzClient`, or None when Qobuz isn't usable.

    Never raises: Qobuz is one tier of a cascade, and an unconfigured or
    unreachable store must simply mean "this tier contributes nothing"."""
    try:
        from utils.config import Config
        from utils.streamrip_api import get_config_path
        from utils.qobuz import QobuzClient
        client = QobuzClient(Config(get_config_path()))
        await client.login()
        return client
    except Exception as exc:
        logger.warning("Qobuz tier unavailable (%s: %s)", type(exc).__name__, exc)
        return None


async def close_client(client) -> None:
    if client is None:
        return
    try:
        await client.close()
    except Exception as exc:
        logger.debug("Qobuz client close failed: %s", exc)
