"""Coarse genre taxonomy — the hand-curated mapping from free-text genre tags
to a small set of families.

Split out of `pca_engine` (which imports numpy at module scope) so the metadata
layer — `genre_similarity`, and through it the walk's hot path — can reach the
taxonomy while staying pure stdlib, exactly like the rest of the code that has
to compile for Android. `pca_engine` re-exports every name here, so existing
`from utils.pca_engine import genre_bucket` imports keep working unchanged.

Two views of the same rules:
  • `genre_bucket`  — ONE family (first rule that matches wins). Priority order
    is meaningful; see the comment on `_GENRE_RULES`.
  • `genre_tokens`  — ALL matching families. Source tags are comma-lists
    ('Rock, Metal, Pop') and collapsing them to a single winner is decided by
    rule order rather than by the music, so prefer this wherever a set will do.
"""

from __future__ import annotations

# Coarse buckets over the messy, multi-label, partly-French Qobuz genre tags.
# Priority order matters (first match wins), and it encodes two constraints:
#   1. Rare / historically-failing genres are matched FIRST within their family
#      so they surface with their own colour instead of being swallowed by the
#      Rock/Pop majority co-tags ("Pop, Rock, Metal" → Metal).
#   2. Electronic / Classical / Folk-Cntry are kept at their ORIGINAL top
#      priority: the walk's regional-scene test (`track_graph._is_regional`)
#      special-cases the regional buckets (Folk/Cntry, Latin, Reggae, Asian-Pop),
#      so a bucket that preceded them could silently change which seeds get the
#      country pool constraint. The niche buckets below (Jazz…Asian-Pop) all sit
#      *after* the majority families, so they only add resolution to display,
#      normalization and the genre diagnostic. Substring collisions are avoided
#      by ordering: Latin before Reggae ('reggaeton' ⊃ 'reggae'), the niche
#      block after Soul ('jazz funk' stays Soul via 'funk'), Asian-Pop before
#      Pop ('k-pop' ⊃ 'pop').
# Language/country-bound scene tags, matched BEFORE everything else.
#
# Two jobs. First, priority: these lose to a generic substring further down the
# list — 'entechno' (Greek art-song) contains 'techno', so it was bucketing as
# Electronic. Matching them first fixes that class of collision.
#
# Second, and the reason the set exists separately from the Folk/Cntry bucket:
# `track_graph._is_regional` gates a HARD cross-country pool constraint, and it
# used to ask "is this the Folk/Cntry bucket?". But that bucket holds both
# genuinely regional scenes (laiko, rebetiko) and borderless Western roots music
# (blues, country, americana, folk-rock). So a GB blues-rock band was treated as
# a regional act and fenced from every non-GB artist — measured at 4.9% of all
# enriched pairs, Fleetwood Mac against essentially the whole library. A scene
# is regional when it travels with a language, not when it is folk-adjacent.
_REGIONAL_KEYS = (
    "laiko", "laika", "laïko", "laïka", "laiki", "λαϊκό", "λαϊκά",
    "rebetiko", "ρεμπέτικο", "entechno", "έντεχνο", "greek folk",
    "musiques du monde", "world music", "worldbeat",
    # Other unmistakably language/country-bound scenes. Kept deliberately
    # short and distinctive — each must be a string that cannot hide inside an
    # unrelated tag once separators are stripped ('rai' is excluded for exactly
    # that reason). Cesária Evora surfaced the need: she is as regional as an
    # artist gets, but scored as borderless because Cape Verde's own genres
    # were absent from this list.
    "morna", "coladeira", "fado", "enka", "bhangra",
)

_GENRE_RULES = [
    ("Folk/Cntry", _REGIONAL_KEYS),
    ("Classical",  ("classical", "classique")),
    ("Hip-Hop",    ("rap", "hip hop", "hip-hop", "hiphop", "trap", "хип", "рэп", "grime", "boom bap", "drill")),
    ("Electronic", ("électronique", "electronica", "electro", "électro", "house", "techno", "edm", "trance", "drum & bass", "dnb", "dubstep", "ambient")),
    ("Folk/Cntry", ("folk", "country", "blues", "bluegrass", "americana", "laiko", "laika", "laïko", "laïka", "laiki", "λαϊκό", "λαϊκά", "rebetiko", "ρεμπέτικο", "entechno", "έντεχνο", "greek folk", "world", "musiques du monde")),
    ("Soul/R&B",   ("soul", "r&b", "funk", "rnb", "motown", "neo soul")),
    ("Jazz",       ("jazz", "bebop")),
    ("Latin",      ("latin", "salsa", "reggaeton", "bachata", "cumbia", "merengue", "bossa nova", "samba", "flamenco")),
    ("Reggae",     ("reggae", "dancehall", "ragga")),
    ("Gospel",     ("gospel", "worship")),
    ("Soundtrack", ("soundtrack", "film score")),
    ("Disco",      ("disco",)),
    ("Metal",      ("metal", "métal", "hard rock", "grunge", "heavy metal")),
    ("Rock/Alt",   ("rock", "alternatif", "alternative", "indé", "indie", "punk", "new wave", "post-punk", "рок")),
    # Keys are matched as raw substrings against tags that have had separators
    # stripped ('folk pop' → 'folkpop'), so bare 'kpop'/'cpop' are UNSAFE — they
    # hide inside 'folkpop', 'darkpop', 'psychedelicpop'. Only distinctive or
    # hyphen-kept forms: 'jpop' (no English word ends in 'j'), 'mandopop',
    # 'cantopop', and the hyphenated 'k-pop'/'c-pop' for raw display strings.
    ("Asian-Pop",  ("k-pop", "j-pop", "jpop", "mandopop", "cantopop", "c-pop")),
    ("Pop",        ("pop", "поп", "greek pop", "greek")),
]

_GENRE_PALETTE = {
    "Rock/Alt": "#40C4FF", "Pop": "#FF80AB", "Metal": "#FF5252",
    "Hip-Hop": "#FFD740", "Electronic": "#76FF03", "Folk/Cntry": "#B388FF",
    "Classical": "#FFFFFF", "Soul/R&B": "#FF6E40",
    "Jazz": "#26C6DA", "Latin": "#FF7043", "Reggae": "#66BB6A",
    "Gospel": "#BA68C8", "Soundtrack": "#5C6BC0", "Disco": "#F50057",
    "Asian-Pop": "#E040FB",
    "Other": "#888888", "Unknown": "#444444",
}

# The exact set of coarse bucket labels genre_bucket can emit. Used to recognise
# a stored genre that is *itself* a bucket label — i.e. an artifact of the old
# collapsing normalization — so it can be re-derived to a finer display tag.
GENRE_BUCKET_LABELS = frozenset(label for label, _ in _GENRE_RULES) | {"Other", "Unknown"}

# Non-informative sentinels that `genre_bucket` / `genre_tokens` can emit.
NON_FAMILIES = frozenset({"Other", "Unknown"})


def genre_bucket(genre: str | None) -> str:
    """Map a free-text (multi-label) genre tag to one coarse bucket."""
    g = (genre or "").strip().lower()
    if not g:
        return "Unknown"
    for label, keys in _GENRE_RULES:
        if any(k in g for k in keys):
            return label
    return "Other"


def genre_tokens(genre: str | None) -> set:
    """Multi-label canonical genre set (FR/RU aware). Unlike genre_bucket's
    first-match single label, returns ALL matching buckets — 39% of library
    tags carry ≥2 genres. {'Unknown'} for empty, {'Other'} for unmatched."""
    g = (genre or "").strip().lower()
    if not g:
        return {"Unknown"}
    toks = {label for label, keys in _GENRE_RULES if any(k in g for k in keys)}
    return toks or {"Other"}


def genre_families(tags) -> frozenset:
    """The coarse families spanned by an iterable of genre tags — each tag
    contributing its PRIMARY family only — with the 'Other'/'Unknown' sentinels
    dropped. Empty when nothing is recognised.

    This is the taxonomy view the metadata gate needs: it answers "are these two
    things even in the same part of the map?" without consulting any learned
    model.

    Primary (`genre_bucket`) rather than multi-label (`genre_tokens`) on purpose.
    Enrichment tokens arrive with separators stripped ('alternative hip hop' →
    'alternativehiphop'), and the rules match raw substrings, so the multi-label
    view leaks generic families into specific tags: 'alternativehiphop' picks up
    Rock/Alt via 'alternative', which was enough to call Kendrick Lamar and
    Slipknot same-family. The priority order exists precisely to resolve that —
    it puts the rare/specific family first — so one primary label per tag is the
    robust signal here. Use `genre_tokens` where you want the full label set
    (display, grouping); use this where a wrong link is costly."""
    fams = set()
    for t in tags or ():
        fams.add(genre_bucket(t))
    return frozenset(fams - NON_FAMILIES)


# Coarse buckets that are regional as a whole — a Latin / Reggae / Asian-Pop
# scene travels with its language and country the way laiko does.
REGIONAL_BUCKETS = frozenset({"Latin", "Reggae", "Asian-Pop"})


def is_regional_tag(genre: str | None) -> bool:
    """True iff a genre tag names a scene tied to a language/country.

    Either an explicitly regional key (`_REGIONAL_KEYS` — laiko, rebetiko,
    entechno, …) or a wholly-regional bucket (Latin, Reggae, Asian-Pop).
    Deliberately NOT the whole Folk/Cntry bucket: blues, country, bluegrass and
    americana are borderless, and treating them as regional made the walk's
    country constraint fence Western roots artists apart by nationality."""
    g = (genre or "").strip().lower()
    if not g:
        return False
    if any(k in g for k in _REGIONAL_KEYS):
        return True
    return genre_bucket(g) in REGIONAL_BUCKETS


def genre_display_label(genre: str | None) -> str:
    """Human-facing label for a genre tag: the coarse bucket when the tag maps to
    one, else the raw tag itself (title-cased) so a genuinely novel genre reads
    as *what it is* instead of a flat 'Other'. Empty/None → 'Unknown'.

    This is the display counterpart to `genre_bucket`, which keeps 'Other' as a
    real sentinel for logic (the `fix_and_normalize` back-fill, the silhouette
    diagnostic's catch-all class). Use this one anywhere a person reads the
    result — it never surfaces 'Other'."""
    g = (genre or "").strip()
    if not g:
        return "Unknown"
    return g.title() if genre_bucket(g) == "Other" else genre_bucket(g)
