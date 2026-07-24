"""
Track graph: acoustic geometry + metadata edges over the music library.

Two things live here:

  • acoustic  — a continuous Zr coordinate space: the z-scored, Kaiser-truncated
                PCA projection of the DSP feature vectors persisted by
                dsp.analyze_track(). Proximity is COSINE over those coordinates
                (which are centred, so it is effectively a correlation). There
                is no persisted edge table any more — `build_acoustic_edges`
                writes coordinates, and the walk ranks against them live.
  • metadata  — same-artist co-occurrence (edge_kind 'artist'). Weight is fixed
                at 1.0; ordering falls back to library order.

The graph is the navigation backbone for the assistant: it routes 'play
something similar' to a seed-ranked queue over the acoustic geometry (see
`walk`), and 'more by this artist' to artist neighbours. Provides the continuous
proximity the assistant needs instead of discrete buckets.

The walk (`walk`, "Seed-Anchored Similarity Queue"):
  • METADATA DEFINES THE POOL, ACOUSTICS ORDER IT. `_pool_foreign` decides
    membership from the tags (genre boundary, plus a country boundary for
    regional seeds and for seeds with no tags at all); everything inside the
    pool is then ranked by proximity to the SEED;
  • it RANKS, it does not chain. A greedy trajectory (step to the best
    neighbour of the current track, repeat) measured strictly worse than plain
    seed-ranking on purity, artist diversity AND closeness to the seed, because
    each wrong step became the next step's anchor;
  • NO metadata term in the score, at all. Membership is the whole of its job;
    the additive genre-continuity and shared-country bonuses that used to sit
    here were measured to add +0.4 points of on-family purity over the gate
    alone while costing diversity, seed anchoring, and biasing hard toward
    artists MusicBrainz happens to have tagged;
  • per-artist / per-album repeat caps bound how much of a queue one act or
    release may take (this replaced an MMR term that moved 2.5% of picks);
  • graceful degradation — with no enrichment nothing is foreign, so the queue
    is pure DSP proximity. That is the intended behaviour for an untagged
    library, not a fallback: with no metadata, proximity is the whole signal.

(Two earlier walkers lived here and were removed: a stochastic
Personalised-PageRank sampler, then the greedy seed-anchored trajectory above.)

All builders are async (DB-bound) but the numpy work runs synchronously —
no off-thread call is necessary for libraries up to ~20K tracks; the SVD and the
σ pass are bandwidth-limited and finish in under a second. For very large
libraries the caller should wrap build_acoustic_edges in asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import numpy as np

from utils.dsp import (
    GRAPH_EMBED_DIMS,
    FEATURES_VERSION,
    analyze_track,
    unpack_graph_embedding,
)

logger = logging.getLogger(__name__)

# ── Coordinate-graph cache ───────────────────────────────────────────────────
# The walk's similarity oracle is the coordinate graph (persisted Zr coords +
# the self-tuning σ vector). Building it does one O(N²) pass
# (load_live_coordinate_graph); without a cache that ran on *every* walk() call
# — i.e. every Play-Similar press recomputed local bandwidths for the whole
# library. We cache it for the process lifetime, keyed on the db_manager
# identity, and invalidate whenever build_acoustic_edges rewrites the geometry.
_COORD_GRAPH_CACHE: dict = {"key": None, "graph": None}


def invalidate_coord_graph_cache() -> None:
    """Drop the cached coordinate graph. Called after any rebuild that changes
    the persisted Zr coords / clusters, so the next walk reloads fresh."""
    _COORD_GRAPH_CACHE["key"] = None
    _COORD_GRAPH_CACHE["graph"] = None


async def _coord_graph_cached(db_manager):
    """`load_live_coordinate_graph` with a process-lifetime cache. Keyed on the
    db_manager identity so two live DBs (e.g. across tests) never share a graph;
    build_acoustic_edges invalidates on rebuild. Returns None (and does not
    cache) when the backend can't serve coordinates — the caller falls back to
    the edge table."""
    key = id(db_manager)
    if _COORD_GRAPH_CACHE["key"] == key and _COORD_GRAPH_CACHE["graph"] is not None:
        return _COORD_GRAPH_CACHE["graph"]
    graph = await load_live_coordinate_graph(db_manager)
    if graph is not None:
        _COORD_GRAPH_CACHE["key"] = key
        _COORD_GRAPH_CACHE["graph"] = graph
    return graph


# Top-K metadata neighbours stored per track per kind. Albums rarely have
# more than ~15 tracks; artists can have hundreds, but the assistant only
# samples a handful at a time.
DEFAULT_K_METADATA = 30

# Edge-kind tags written into track_neighbors.edge_kind.
KIND_ACOUSTIC = "acoustic"
KIND_ARTIST = "artist"
KIND_ALBUM = "album"


# ── Builders ─────────────────────────────────────────────────────────────────


# Canonical order of the scalar descriptors appended to the timbre block
# (EMBED_DIMS floats: mfcc mean/std/delta + chroma + rhythm, see dsp.py).
# `bpm` denotes the log2(bpm) column.
#
# ── Why the Camelot key coords are NOT here ──────────────────────────────────
# Three harmonic columns (cos_h, sin_h, key_mode) used to be appended, held out
# of the SVD and late-fused back at weight 1.5 so PCA could not rotate the rigid
# Camelot-wheel geometry. They were deleted: key carries no genre information,
# and spending ~3 of 21 Zr dimensions (amplified 1.5×) on it measurably blurred
# the metric. Leave-one-artist-out same-family purity over the real library, by
# harmonic weight:
#
#     weight   top-1    top-5    top-10
#     0.00     86.5%    86.0%    85.1%     <- deleted
#     0.50     86.5%    86.1%    84.9%
#     1.00     85.2%    85.9%    84.6%
#     1.50     85.1%    84.9%    83.7%     <- what shipped
#     2.00     85.5%    84.0%    82.9%
#
# Monotone. Confirmed independently on held-out artist splits (Zr with the block
# 86.4%/85.2% top-1/top-10, without it 88.4%/86.2%). If harmonic mixing ever
# becomes a real feature, reintroduce key as its OWN ranking pass rather than as
# dimensions inside the similarity metric — weight ≤0.5 is free but also does
# nothing, which is the worst of both.
_SCALAR_ORDER = (
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast",
)


def _all_scalars(row: dict) -> dict[str, float]:
    """Every scalar descriptor for one track row, keyed by `_SCALAR_ORDER`.
    `bpm` is returned as log2(bpm)."""
    bpm_raw = float(row.get("bpm", 0) or 0)
    return {
        "bpm": float(np.log2(max(bpm_raw, 1.0))),
        "brightness": float(row.get("brightness", 0) or 0),
        "energy": float(row.get("energy", 0) or 0),
        "rolloff": float(row.get("rolloff", 0) or 0),
        "beat_strength": float(row.get("beat_strength", 0) or 0),
        "spectral_flatness": float(row.get("spectral_flatness", 0) or 0),
        "spectral_contrast": float(row.get("spectral_contrast", 0) or 0),
    }


def _surviving_scalars(redundant: set[str]) -> list[str]:
    """Scalar names that survive covariance cleaving, in canonical order."""
    return [s for s in _SCALAR_ORDER if s not in redundant]


def _feature_vector(row: dict, timbre: np.ndarray, surviving: list[str]) -> np.ndarray:
    """Full graph feature vector for one track: the EMBED_DIMS timbre block
    followed by the surviving scalar descriptors in `surviving` order."""
    sc = _all_scalars(row)
    scalars = np.array([sc[s] for s in surviving], dtype=np.float32)
    return np.concatenate([timbre.astype(np.float32), scalars])


async def build_acoustic_edges(
    db_manager,
    features_version: int = FEATURES_VERSION,
    z_score: bool = True,
    scalar_weight: float = 1.5,
) -> int:
    """Recompute the acoustic geometry from scratch.

    Loads every track that has a current-version feature BLOB, z-scores the
    vectors (or merely centres them if z_score=False), Kaiser-truncates them
    with an SVD, and persists the resulting Zr coordinates. Returns the number
    of tracks projected.

    Coverage degrades gracefully: tracks without features are simply absent
    from the geometry. The assistant falls back to metadata edges for those.
    """
    rows = await db_manager.get_tracks_with_features(features_version)
    if len(rows) < 2:
        logger.info("track_graph: acoustic projection skipped (only %d tracks with features)", len(rows))
        return 0

    # ── Metadata enrichment is DECOUPLED from the acoustic build ─────────────
    # The acoustic geometry (timbre + dynamics + harmony) does not depend on
    # artist country/genre at all — enrichment only feeds the walk's metadata
    # gate and the NPMI genre model, which is (re)built from whatever enrichment
    # exists at the end of this function and refreshed again by the background
    # enrichment task (main._enrich_metadata_async) as provenance lands.
    # Triggering MusicBrainz here forced a rate-limited (1 req/s) network stall
    # onto every graph rebuild for no geometric benefit; it now runs off the
    # hot path.

    # ── Feature selection: drop covariance-redundant scalars ──────────────
    # The geometry uses every feature that survives the unsupervised PCA /
    # Pearson-covariance analysis. Collinear scalars (e.g. rolloff ↔ brightness)
    # are cleaved so they don't double-count toward distance. The 68-D graph
    # embedding timbre block is structural and always kept; only the raw scalar
    # descriptors are subject to cleaving.
    from utils.pca_engine import redundant_raw_features
    redundant = redundant_raw_features(rows)
    if redundant:
        logger.info(
            "track_graph: covariance analysis cleaved redundant scalars %s "
            "from the graph feature space", sorted(redundant),
        )

    surviving = _surviving_scalars(redundant)
    paths: list[str] = []
    vectors: list[np.ndarray] = []
    for i, r in enumerate(rows):
        # The graph embedding is the v4 BLOB with mfcc_delta removed
        # (GRAPH_EMBED_DIMS): the ablation showed delta is dead weight for
        # similarity. Old/short BLOBs unpack to None and are skipped.
        v = unpack_graph_embedding(r.get("timbre"))
        if v is None or v.shape[0] != GRAPH_EMBED_DIMS:
            continue
        # timbre block + the surviving scalar descriptors (tempo as
        # log2(bpm), key as cos_h/sin_h/mode) so dynamics and harmony shape
        # similarity alongside timbre. See `_all_scalars` / `_feature_vector`.
        paths.append(r["path"])
        vectors.append(_feature_vector(r, v, surviving))
        # Cooperative yield. serious_python is single-threaded CPython, so this
        # per-track assembly otherwise holds the asyncio loop for its whole
        # duration on a large first-load build — long enough to freeze the UI's
        # windowed-scroll slides. Hand control back every few hundred rows so
        # Flet's bridge can flush pending updates between chunks.
        if i % 512 == 0:
            await asyncio.sleep(0)

    if len(vectors) < 2:
        return 0

    X = np.stack(vectors, axis=0)  # (N, EMBED_DIMS + len(surviving))

    # z-score the feature matrix (or merely centre it when z_score=False).
    mu = X.mean(axis=0)
    if z_score:
        sd = X.std(axis=0)
        sd = np.where(sd < 1e-8, 1.0, sd)
    else:
        sd = np.ones(X.shape[1], dtype=X.dtype)
    Z = (X - mu) / sd

    # Boost the non-timbre scalars AFTER z-scoring so tempo/dynamics carry
    # weight comparable to the individual timbre axes.
    n_scalars = Z.shape[1] - GRAPH_EMBED_DIMS
    if scalar_weight != 1.0 and n_scalars > 0:
        Z[:, GRAPH_EMBED_DIMS:] *= scalar_weight

    # ── PCA reduction (Kaiser-truncated SVD) ────────────────────────────────
    # Everything enters the SVD now. There used to be a "late fusion" split
    # holding the 3 Camelot columns out of the PCA and concatenating them back
    # afterwards, to protect the wheel's rigid geometry from being rotated into
    # mixed PCs. Those columns are gone entirely (see `_SCALAR_ORDER`), and with
    # them the split — the PCA is measurably NOT the problem it was protecting
    # against: held-out purity is 86.4% through the PCA vs 86.6% on the raw
    # 76-D matrix, and better than raw once the harmonic block is dropped.
    Zr = Z.astype(np.float32)
    N = Z.shape[0]
    V_keep = np.eye(Z.shape[1], dtype=np.float32)
    eigenvalues = np.zeros(Z.shape[1], dtype=np.float32)
    if Z.shape[1] >= 4 and N > 1:
        # SVD is the single heaviest op in the build; run it off the loop
        # (LAPACK releases the GIL) so it can't freeze the UI.
        _U, _S, _Vt = await asyncio.to_thread(
            np.linalg.svd, Z, full_matrices=False,
        )
        eigenvalues = (_S ** 2) / float(N - 1)
        kaiser_k = int((eigenvalues > 1.0).sum())
        kaiser_k = max(3, min(kaiser_k, _Vt.shape[0]))
        V_keep = _Vt[:kaiser_k].T.astype(np.float32)     # (D, kaiser_k)
        Zr = (Z @ V_keep).astype(np.float32)             # (N, kaiser_k)
        cum_var = float(eigenvalues[:kaiser_k].sum() / eigenvalues.sum()) if eigenvalues.sum() > 0 else 0.0
        logger.info(
            "track_graph: PCA-reduced %d dims to %d "
            "(Kaiser λ>1; %.1f%% variance retained)",
            int(_Vt.shape[1]), V_keep.shape[1], cum_var * 100.0,
        )

    # ── Persist the per-track Zr coordinates ────────────────────────────────
    # The coordinates ARE the geometry — there is nothing else to store. (A
    # `pca_space` table used to persist means/stds/V_keep alongside them for an
    # on-demand projection of new tracks that was never built; it is gone, and
    # `db_manager.GEOMETRY_VERSION` now handles invalidating stale coords.)
    if hasattr(db_manager, "update_tracks_pca_coords_batch"):
        try:
            await db_manager.update_tracks_pca_coords_batch(
                [(paths[i], Zr[i]) for i in range(Zr.shape[0])]
            )
            logger.info(
                "track_graph: persisted Zr geometry (%d tracks × %d dims)",
                Zr.shape[0], Zr.shape[1],
            )
        except Exception as exc:
            logger.warning("track_graph: persisting Zr geometry failed: %s", exc)

    # ── Genre-similarity model (NPMI 'genre-BLOSUM') ───────────────────────
    # Precompute + persist from the artist enrichment cache so the walk's
    # metadata gate loads it instead of rebuilding it each session. Non-fatal:
    # no enrichment → empty model → the walk's genre term degrades to Dice.
    try:
        await build_genre_affinity(db_manager)
    except Exception as gerr:
        logger.warning("track_graph: genre affinity build skipped (%s)", gerr)

    # Genre-adjacency graph for the journey walk. Rides here because the Zr
    # coords it reads are now current. Non-fatal: on failure the walk falls back
    # to the pure-radius ranking.
    try:
        await build_journey_graph(db_manager)
    except Exception as jerr:
        logger.warning("track_graph: journey graph build skipped (%s)", jerr)

    # The persisted Zr coords / clusters just changed, so any cached coordinate
    # graph the walk is holding is stale — force the next walk to reload it.
    invalidate_coord_graph_cache()

    return Zr.shape[0]


async def build_genre_affinity(db_manager) -> int:
    """(Re)build + persist the NPMI genre-similarity model from the artist
    enrichment cache, so the walk's metadata gate loads a precomputed model
    rather than recomputing it per session. Returns the number of genre pairs
    stored — a graceful 0 when the backend lacks the enrichment accessors (test
    fakes) or there's no enrichment yet."""
    if not (
        hasattr(db_manager, "get_all_artist_genre_sets")
        and hasattr(db_manager, "save_genre_affinity")
    ):
        return 0
    from utils.genre_similarity import build_npmi_model
    token_sets = await db_manager.get_all_artist_genre_sets()
    model = build_npmi_model(token_sets)
    await db_manager.save_genre_affinity(model)
    logger.info(
        "track_graph: genre affinity model built (%d pairs from %d tagged artists)",
        len(model), len(token_sets),
    )
    return len(model)


async def build_journey_graph(db_manager) -> int:
    """(Re)build + persist the genre-adjacency graph the journey walk traverses:
    regional-aware PAGA nodes (coarse family + country split, untagged tracks
    placed by label-propagation) plus their kNN-connectivity adjacency.

    Rides the same rebuild path as `build_genre_affinity` — the Zr coords it
    reads are already current — and is cheap (block-chunked kNN + one
    connectivity pass). Returns the placed-track count; a graceful 0 when the
    backend lacks the accessors (test fakes) or there are no coordinates yet, in
    which case the walk falls back to the pure-radius ranking."""
    if not (
        hasattr(db_manager, "save_journey_graph")
        and hasattr(db_manager, "get_artist_meta_for_paths")
    ):
        return 0
    from utils import genre_graph as gg

    graph = await load_live_coordinate_graph(db_manager)
    if not graph or not graph.get("paths"):
        await db_manager.save_journey_graph({})
        return 0

    paths = graph["paths"]
    meta_map = await db_manager.get_artist_meta_for_paths(paths)
    meta = [
        {
            "genres": (meta_map.get(p) or {}).get("genres"),
            "country": (meta_map.get(p) or {}).get("country"),
        }
        for p in paths
    ]
    g = gg.build_genre_graph(graph["X_unit"], meta)
    payload = {
        "version": 1,
        "nodes": {paths[i]: g["nodes"][i] for i in range(len(paths))},
        "adj": {n: [[b, float(l)] for b, l in v] for n, v in g["adj"].items()},
    }
    await db_manager.save_journey_graph(payload)
    logger.info(
        "track_graph: journey graph built (%d tracks, %d nodes, %d edges, %d inferred)",
        len(paths), len(g["sizes"]),
        sum(len(v) for v in g["adj"].values()), sum(g["inferred"]),
    )
    return len(paths)


async def build_metadata_edges(
    db_manager,
    k: int = DEFAULT_K_METADATA,
) -> tuple[int, int]:
    """Recompute the metadata tier. Same-artist pass.

    Returns (artist_edge_count, album_edge_count).
    """
    conn = await db_manager.get_connection()

    # Same-artist: any other track by the same artist. Cap per-source to k
    # so a prolific artist doesn't write thousands of rows for one source.
    artist_edges: list[tuple[str, str, float]] = []
    sql_art = '''
        SELECT ar.id AS artist_id, t.path, t.added_date
        FROM tracks t
        JOIN albums  al ON al.id = t.album_id
        JOIN artists ar ON ar.id = al.artist_id
        ORDER BY ar.id, t.added_date DESC, t.path
    '''
    async with conn.execute(sql_art) as cursor:
        rows = await cursor.fetchall()

    by_artist: dict[int, list[str]] = {}
    for r in rows:
        by_artist.setdefault(r["artist_id"], []).append(r["path"])
    for gi, paths in enumerate(by_artist.values()):
        if len(paths) < 2:
            continue
        # For each source, write up to k other tracks. Sampling is biased
        # towards the most-recently-added tracks (the SQL is already ordered
        # that way) — the assistant phrasings 'more by this artist' almost
        # always mean 'newer stuff first'.
        for src in paths:
            others = [p for p in paths if p != src]
            for dst in others[:k]:
                artist_edges.append((src, dst, 1.0))
        # Cooperative yield (see build_acoustic_edges): a prolific-artist library
        # makes this inner loop heavy enough to stall UI scroll on the single
        # event-loop thread. Release it periodically.
        if gi % 128 == 0:
            await asyncio.sleep(0)

    await db_manager.replace_neighbors_bulk(artist_edges, KIND_ARTIST)

    logger.info(
        "track_graph: wrote %d artist + 0 album metadata edges",
        len(artist_edges),
    )
    return len(artist_edges), 0


# ── Traversal primitives ─────────────────────────────────────────────────────


async def neighbors(
    db_manager,
    track_path: str,
    k: int = 10,
    edge_kind: Optional[str] = None,
) -> list[dict]:
    """Top-k neighbours of `track_path`, joined with track metadata.

    edge_kind=None returns the highest-weighted edges across all tiers
    (acoustic neighbours generally rank above metadata ones because cosine
    weights tend to sit in [0.7, 0.99] while metadata weights are ≤ 1.0).
    """
    return await db_manager.get_neighbors(track_path, k=k, edge_kind=edge_kind)

# ── Regional scenes (the ONE genre-taxonomy fact the walk still needs) ────────
# A regional scene travels with a country/language, so for such a seed a foreign
# country IS foreign — even a moderate cross-country genre overlap (laiko↔
# dance-pop via NPMI) is the wrong continuation, and the right one (another
# same-country track) is often *untagged*, so genre-set similarity can't rank it.
# `_pool_foreign` therefore treats country as a HARD pool constraint for regional
# seeds. Everything else (Hip-Hop, Rock, Pop, Metal, Electronic, Jazz, …) is a
# borderless/international scene where nationality says little, so country there
# is simply not consulted.
#
# The membership test is per-TAG (`genre_taxonomy.is_regional_tag`), not per
# coarse bucket: Folk/Cntry holds laiko/rebetiko (regional) AND blues, country,
# americana, folk-rock (borderless), so gating on the whole bucket fenced Western
# roots artists apart by nationality — Fleetwood Mac against essentially the
# whole library, 4.9% of all enriched pairs in the audit.

# ── Why metadata NEVER enters the ordering score ─────────────────────────────
# Two additive metadata terms used to sit alongside the acoustic score: a
# seed-anchored shared-country bonus (`_meta_score`, λ=0.35) and a
# current-anchored NPMI genre-continuity term (`_genre_flow`, λ=0.30). Both are
# deleted. Measured over 80 seeds on the real library, against the pool gate
# that was already running:
#
#     greedy, acoustic only            on-family 84.5%  artists 8.0  seed-aff .275
#     greedy + `_pool_foreign` only    on-family 99.6%  artists 7.8  seed-aff .270
#     + both metadata score terms      on-family 100.0% artists 7.6  seed-aff .246
#
# The gate does the genre work (84.5% → 99.6%). The score terms bought +0.4
# points for a measurable loss of diversity and seed anchoring, while deciding
# 43% (genre) and 16% (country) of all picks.
#
# They were also actively biased. `_genre_flow` returned 0.0 when either side
# lacked tags — intended as graceful degradation, but under an arg-max 0.0 is
# not neutral, it is last place. 27% of candidate evaluations scored 0 and 66%
# of those zeros were an UNTAGGED candidate rather than a different genre, so
# the tagged share of picks ran at 93% against an 80% tagged pool. With 60% of
# the GR catalogue untagged, the term fenced out the very scene the country
# rule in `_pool_foreign` exists to protect.
#
# The deeper reason no λ could have worked: the acoustic term is
# exp(-d²/(σᵢσⱼ)), self-tuning, so its spread across a candidate pool scales
# with local density, while λ·gx is fixed on [0,1]. In the dense hip-hop
# majority the acoustic spread compresses and the fixed term wins; in sparse
# regions it is noise. Metadata now does exactly one job: pool membership.


def _is_regional(genres) -> bool:
    """True iff a seed's genre profile is DOMINATED by a scene tied to a
    language/country — i.e. at least half its tags are regional.

    Dominance, not presence. Presence alone over-fires on borderless artists who
    happen to carry one regional-flavoured tag, and because this gates a HARD
    cross-country pool constraint, one stray tag was enough to fence an artist
    from the entire rest of the library by nationality: AJ Tracey (a UK rapper
    tagged cloud rap / grime / hip hop / UK drill / *dancehall*) was fenced from
    other UK rappers on the strength of that single dancehall tag.

    The threshold is calibrated on the real enrichment, where the split is
    unusually clean — genuinely regional acts sit at 0.50-1.00 (Vasilis Karras
    laiko+rebetiko 0.67, Giorgos Mazonakis 0.50) and borderless ones at 0.12-0.25
    (Santana latinrock 0.12, Pitbull latinpop 0.12, AJ Tracey dancehall 0.20).
    A majority rule lands in the gap.

    Note the two failure directions are NOT symmetric, which is why a majority
    (rather than a lower bar) is right: a false positive fences a track out of
    the queue on nationality alone, while a false negative merely falls back to
    the genre test, which still catches a genuine scene jump (Carti -> laiko is
    fenced on genre similarity 0.000, never needing this rule)."""
    if not genres:
        return False
    from utils.genre_taxonomy import is_regional_tag
    regional = sum(1 for g in genres if is_regional_tag(g))
    return regional * 2 >= len(genres)


# Cache of credit-string → member-key frozenset. The walk asks the same-act
# question once per candidate, and decomposition is pure string work.
_CREDIT_KEY_CACHE: dict[str, frozenset] = {}


def _credit_key_set(name) -> frozenset:
    """Member keys of one artist CREDIT STRING, memoised. '21 Savage & Metro
    Boomin' decomposes to both members, so the walk's per-artist cap counts a
    collab against each act it names rather than treating it as a new artist."""
    if not name:
        return frozenset()
    keys = _CREDIT_KEY_CACHE.get(name)
    if keys is None:
        from utils.metadata_enrich import credit_keys
        keys = credit_keys(name)
        _CREDIT_KEY_CACHE[name] = keys
    return keys


def _same_act(a_name, b_name) -> bool:
    """True iff two artist CREDIT STRINGS name (at least partly) the same
    performer.

    Raw string equality is not enough: streaming sources credit the same artist
    as '21 Savage', '21 Savage & Metro Boomin' and
    'Travis Scott/Metro Boomin/21 Savage', each of which becomes its own
    `artists` row with its own enrichment. Under string equality the walk's
    same-artist guards silently never fired for those rows, so an artist's own
    collab track could be scored as a stranger — and, when its enrichment
    resolved to the wrong entity, vetoed out of that artist's own queue.

    Membership overlap fixes it in the safe direction: the guard it feeds (the
    `_pool_foreign` exemption) only ever ADMITS a track, so a false positive
    costs one loosely-related track while a false negative fences out a genuine
    one."""
    if not a_name or not b_name:
        return False
    if a_name == b_name:
        return True
    return bool(_credit_key_set(a_name) & _credit_key_set(b_name))


def _pool_foreign(
    seed_path: str, cand_path: str, meta_map: dict, genre_model: dict, floor: float,
) -> bool:
    """Pool membership test: True iff `cand` is *known* to be foreign to the
    seed and must be excluded from the walk's candidate pool outright — a strong
    timbre bridge must never carry the queue across this boundary.

    Two evidence-gated boundaries, either one foreign-marks the candidate:
      • GENRE — both carry genre tokens and their NPMI soft-set similarity is
        below `floor` (the Carti→laiko timbre-bridge across an obvious genre gap).
      • COUNTRY — both tracks carry a country, the countries differ, and the
        SEED is either a regional scene (`_is_regional`) or has NO GENRE TAGS
        AT ALL. For laiko/Latin/Reggae/Asian-Pop, cross-country IS the genre
        jump, and it catches the case genre can't: the right same-country
        continuation is frequently untagged, so only country separates it from
        an acoustically-near foreign-pop track.

        The untagged-seed clause closes a hole that made the whole gate vacuous
        for a sixth of this library. Both boundaries demand evidence on the SEED
        side, so a seed with no tags used to pass BOTH tests trivially and walk
        with no pool constraint whatsoever — the opposite of the intended
        conservatism, because "we know nothing about this seed" was read as
        "nothing is foreign to it" rather than "we cannot tell what is". Measured
        on the real library: 25% of enriched artists come back status='ok',
        score=100 with an EMPTY genre list (MusicBrainz simply has no tags for
        them), and 60% of the GR-country catalogue is in that set — so the
        untagged seeds are not a random sample, they are one scene. A Negros Tou
        Moria seed walked straight out of Greek rap into Don Toliver, Metro
        Boomin, 21 Savage and Nipsey Hussle; with this clause it holds the scene
        (Mad Clip, RACK, Snik, Toquel, Dani Gambino).

        Country is the right fallback specifically BECAUSE the tags are missing:
        it is the one provenance field these artists do carry, and an untagged
        artist in a tagged library is overwhelmingly a local-scene act that
        MusicBrainz has not catalogued. Note this is strictly narrower than it
        looks — it needs a country on BOTH sides, so it never fires for the
        7% of tracks with no country at all.

    Conservative: fires only on positive evidence (both sides tagged on the
    relevant field, same artist exempt). Missing enrichment → not foreign, so an
    unenriched library degrades to the pure acoustic flow rather than an empty
    queue."""
    if floor <= 0.0:
        return False
    from utils.genre_similarity import soft_set_sim
    ms = meta_map.get(seed_path)
    mc = meta_map.get(cand_path)
    if not ms or not mc:
        return False
    if _same_act(ms.get("artist"), mc.get("artist")):
        return False  # same artist is never foreign
    gs = ms.get("genres") or frozenset()
    gc = mc.get("genres") or frozenset()
    # Genre boundary (needs tags on both sides).
    if gs and gc and soft_set_sim(gs, gc, genre_model) < floor:
        return True
    # Country boundary. Fires for a regional-scene seed, and ALSO for a seed
    # with no genre tags at all — see below.
    cs, cc = ms.get("country"), mc.get("country")
    if cs and cc and cs != cc and (_is_regional(gs) or not gs):
        return True
    return False


async def _journey_queue(
    db_manager, coord_graph, seed_idx, seed_path, length, exclude,
    max_per_artist, max_per_album,
):
    """Build the queue by traversing the persisted genre-adjacency graph: a leg
    in the seed's genre, then a hop into an adjacent genre through its interface
    tracks (see `genre_graph.journey`). Returns None — so `walk` falls back to
    the pure radius — when no genre graph is built yet, the seed can't be placed,
    or anything goes wrong. Repeat caps are enforced inside the traversal, not
    after it, so a leg never truncates on a dominant artist."""
    if not hasattr(db_manager, "get_journey_graph"):
        return None
    payload = await db_manager.get_journey_graph()
    nodes_by_path = (payload or {}).get("nodes") or {}
    adj_raw = (payload or {}).get("adj") or {}
    if not nodes_by_path or not adj_raw:
        return None

    from utils import genre_graph as gg

    paths = coord_graph["paths"]
    nodes = [nodes_by_path.get(p, gg.UNKNOWN_NODE) for p in paths]
    adj = {n: [(b, float(l)) for b, l in v] for n, v in adj_raw.items()}
    display = coord_graph["meta_map"]
    artist_keys = [_credit_key_set((display.get(p) or {}).get("artist")) for p in paths]
    album_keys = [(display.get(p) or {}).get("album") for p in paths]
    p2i = coord_graph["path_to_idx"]
    excl_idx = {p2i[p] for p in exclude if p in p2i}

    # journey counts the seed at index 0, so ask for length+1 and drop it.
    order = gg.journey(
        seed_idx, coord_graph["X_unit"], nodes, adj,
        length=length + 1, hops=1,
        exclude=excl_idx,
        artist_keys=artist_keys, album_keys=album_keys,
        max_per_artist=max_per_artist, max_per_album=max_per_album,
    )
    out = [paths[i] for i in order if paths[i] != seed_path]
    return out[:length] or None


async def load_live_coordinate_graph(db_manager):
    """Load the persisted Zr coordinates + display metadata into RAM and
    L2-normalise them. This is the walk's similarity oracle.

    NO O(N²) pass. Two used to live here:
      • the mutual-kNN affinity thresholds, which existed to stop
        cluster-centroid "hub" tracks dominating a greedy chain — deleted with
        the chain (see `walk`);
      • the Zelnik-Manor self-tuning bandwidths σ, needed by the
        exp(-d²/(σᵢσⱼ)) affinity — deleted with the affinity itself.

    The walk ranks by COSINE now, which needs no bandwidth: Zr is already
    centred (V comes from the SVD of centred data), so cosine here is
    effectively a correlation. Measured on two independent library images,
    leave-one-artist-out same-family purity:

        library A   affinity  top-1 87.7  top-5 85.9  top-10 85.1  top-20 83.6
                    cosine    top-1 88.3  top-5 87.2  top-10 86.1  top-20 85.1
        library B   affinity  top-1 87.6  top-5 86.0  top-10 84.5  top-20 83.4
                    cosine    top-1 87.7  top-5 86.2  top-10 85.6  top-20 84.9

    Same direction on both, and the margin GROWS with k — i.e. it is largest
    over the span a real queue actually occupies. Self-tuning σ was meant to
    stop dense regions dominating; normalising the vectors turns out to do that
    job better here, and reduces graph load from O(N²) to O(N·D).
    """
    rows = await db_manager.get_tracks_pca_coords()
    if not rows:
        return None

    paths = [r["path"] for r in rows if r.get("pca_coords") is not None]
    if not paths:
        return None

    path_to_idx = {p: i for i, p in enumerate(paths)}
    X_zr = np.array([r["pca_coords"] for r in rows if r.get("pca_coords") is not None], dtype=np.float32)

    # Display metadata (title/artist/album/genre) — feeds the walk's repeat caps
    # and the network view. NOT enrichment: the pool gate reads genres/country
    # from get_artist_meta_for_paths instead.
    meta_map = {r["path"]: r for r in rows}

    norms = np.linalg.norm(X_zr, axis=1, keepdims=True)
    X_unit = (X_zr / np.maximum(norms, 1e-9)).astype(np.float32)

    return {
        "X_zr": X_zr,
        "X_unit": X_unit,
        "paths": paths,
        "path_to_idx": path_to_idx,
        "meta_map": meta_map,
    }


async def walk(
    db_manager,
    seed_path: str,
    length: int = 10,
    avoid: Optional[set[str]] = None,
    veto_genre_floor: float = 0.06,
    max_per_artist: int = 2,
    max_per_album: int = 1,
) -> list[str]:
    """Seed-anchored similarity queue: rank the library by acoustic proximity to
    the seed, keep what metadata admits, cap repeats, take the top `length`.

        pool(Seed) = every track NOT `_pool_foreign` to the Seed (genre
                     boundary, or country boundary for regional scenes and
                     untagged seeds), minus `avoid` and the seed itself
        rank(C)    = cosine(Zr_Seed, Zr_C)               # Zr is centred, so
                                                        # this is a correlation

    Metadata decides membership and NOTHING else — see the comment above
    `_is_regional` for the measurements that removed it from the score.

    ── Why this no longer chains ────────────────────────────────────────────
    This used to be a greedy trajectory: step to the best neighbour of the
    CURRENT track under 0.7·Sim(current) + 0.3·Sim(seed), repeat. That was
    measured to be strictly worse than not chaining at all. Over 80 seeds on the
    real library, 10-track queues:

        greedy chain (acoustic only)   on-family 84.5%  artists 8.0  seed-aff .275
        beam-8       (acoustic only)   on-family 86.6%  artists 7.6  seed-aff .292
        seed-ranked  (this)            on-family 87.1%  artists 8.3  seed-aff .380

    Ranking wins on purity, diversity AND closeness to the seed simultaneously.
    The reason is compounding: the top acoustic neighbour shares a coarse genre
    family 86% of the time, so a 10-step chain holds the genre only 0.86¹⁰ ≈ 23%
    of the time — and each wrong step became the new anchor, so one timbre bridge
    took the whole rest of the queue with it. Beam search landing in between is
    the tell that the trajectory OBJECTIVE was the problem, not the search over
    it. With no chain there is nothing to compound, which also deletes the
    machinery that existed only to contain the drift: mutual-kNN hub pruning,
    the dead-end fallback, and the re-anchor fallback.

    It also costs less: one similarity pass instead of `length` of them, and
    `load_live_coordinate_graph` needs no O(N²) pass at all any more (see there
    for why cosine replaced the self-tuning affinity kernel).

    ── Repeat capping (replaces MMR) ────────────────────────────────────────
    `max_per_artist` / `max_per_album` bound how much of the queue one act or one
    release may occupy. This is what the MMR term was reaching for — stop chaining
    remixes, alternate mixes and the same song on another release — done exactly
    rather than approximately: near-duplicates are overwhelmingly same-album, and
    MMR's timbre cosine could not see it (measured, it changed 2.5% of picks,
    because cosine over the raw, NON-CENTRED timbre block spans only 0.56–0.97 —
    note the ranking cosine below is over centred Zr, which does not have that
    compressed-range problem).
    Artist counting goes through `credit_keys`, so '21 Savage' and
    '21 Savage & Metro Boomin' count as one act. Pass 0 to disable either cap.

    Degrades gracefully at every layer: no coordinate graph → rank the seed's
    stored acoustic edges instead; no enrichment → nothing is foreign and the
    queue is pure DSP proximity; no display metadata → no caps.
    """
    exclude: set[str] = set(avoid or set())
    exclude.add(seed_path)

    # The coordinate graph is the similarity oracle: it holds the persisted Zr
    # coords + self-tuning σ, which is what lets us rank the WHOLE library by
    # seed affinity rather than just the seed's stored top-K. Cached across walks
    # (a rebuild invalidates it). A backend without coordinates (the test fakes,
    # or a library that hasn't been built) returns None and we fall back to the
    # persisted edge table.
    coord_graph = None
    try:
        coord_graph = await _coord_graph_cached(db_manager)
    except Exception as exc:
        logger.warning("track_graph.walk: no coordinate graph, using edge table: %s", exc)

    # ── Journey: the primary queue builder ───────────────────────────────────
    # When a genre-adjacency graph has been built, the queue TRAVELS from the
    # seed's genre into an adjacent one rather than staying in a single-genre
    # radius. This is the queue the app ships; the seed-affinity ranking below is
    # now the FALLBACK for when no graph exists yet (fresh library, unbuilt
    # on-device geometry, test fakes) or the seed can't be placed. Any failure
    # inside the journey falls through to that radius — degradation is mandatory
    # (see graph_status: an unbuilt graph must still return a queue, not []).
    seed_idx = coord_graph["path_to_idx"].get(seed_path) if coord_graph else None
    if coord_graph is not None and seed_idx is not None:
        try:
            journeyed = await _journey_queue(
                db_manager, coord_graph, seed_idx, seed_path, length,
                exclude, max_per_artist, max_per_album,
            )
        except Exception as exc:
            logger.warning("track_graph.walk: journey failed, using radius: %s", exc)
            journeyed = None
        if journeyed:
            return journeyed

    # ── Radius fallback: rank every candidate by seed affinity ────────────────
    ranked: list[str] = []
    display: dict[str, dict] = {}
    if coord_graph is not None and seed_idx is not None:
        X_unit = coord_graph["X_unit"]
        all_paths = coord_graph["paths"]
        sim = X_unit @ X_unit[seed_idx]        # cosine to the seed, one matvec
        sim[seed_idx] = -np.inf
        display = coord_graph["meta_map"]
        ranked = [
            all_paths[j] for j in np.argsort(-sim)
            if all_paths[j] not in exclude
        ]
    else:
        # Test-support / unbuilt-library fallback: the seed's stored acoustic
        # edges, already weight-ordered by the accessor.
        rows: list[dict] = []
        if hasattr(db_manager, "get_neighbors_multi"):
            rows = await db_manager.get_neighbors_multi(seed_path, (KIND_ACOUSTIC,), k=200)
        elif hasattr(db_manager, "get_neighbors"):
            rows = await db_manager.get_neighbors(seed_path, k=200, edge_kind=KIND_ACOUSTIC)
        for r in rows:
            p = r.get("path")
            if not p or p in exclude:
                continue
            ranked.append(p)
            display[p] = r

    if not ranked:
        return []

    # ── Node fence: float the seed's genre-node + its adjacencies to the front ─
    # The radius ranks by pure cosine, and the acoustic geometry places foreign
    # genres right next to a seed — measured on the real library, Electronic,
    # Metal and Rock all sit inside Hip-Hop seeds' nearest neighbours (production-
    # timbre bridges). The tag gate below CANNOT fence an untagged candidate (it
    # needs genres on both sides), so ~22% of tagged and ~48% of untagged Hip-Hop
    # seeds leaked a foreign node into the fallback queue. The genre-adjacency
    # partition places EVERY track — untagged ones by label-propagation — so we
    # reuse it: a stable sort floats candidates in the seed's node ∪ its adjacent
    # nodes ahead of foreign ones, which then only appear as padding if the
    # in-genre pool can't fill the queue. This is the same coherence the journey
    # gets for free (its legs are node-restricted); the radius had none.
    # Inactive when no partition is built (seed_node is None) — then the tag gate
    # is the only fence, exactly as before, so an unenriched library still walks
    # on pure acoustics.
    if hasattr(db_manager, "get_journey_graph"):
        try:
            _jpayload = await db_manager.get_journey_graph()
        except Exception:
            _jpayload = None
        _nodes_by_path = (_jpayload or {}).get("nodes") or {}
        _seed_node = _nodes_by_path.get(seed_path)
        if _seed_node:
            _adj = (_jpayload or {}).get("adj") or {}
            _allowed = {_seed_node} | {b for b, _ in _adj.get(_seed_node, [])}
            # Stable sort: False (0) for in-fence keeps them first in cosine order,
            # True (1) sinks foreign nodes to the tail as padding.
            ranked.sort(key=lambda p: _nodes_by_path.get(p) not in _allowed)

    # ── Metadata context (genre-NPMI + country), for the POOL GATE only ──────
    # Fetched per scanned block, not for the whole ranked library. If the backend
    # can't serve enrichment, meta_active latches off and nothing is foreign.
    meta_active = hasattr(db_manager, "get_artist_meta_for_paths")
    meta_map: dict[str, dict] = {}
    genre_model: dict = {}
    # NB: meta_map is filled ONLY from get_artist_meta_for_paths (the
    # {artist, country, genres} shape `_pool_foreign` needs). We do NOT seed
    # it from coord_graph["meta_map"] — those rows carry album-level display
    # fields, not enrichment genres/country, and pre-seeding them made
    # _ensure_meta treat every path as "already present" and skip the real
    # enrichment fetch, silently killing the metadata gate in coordinate mode.
    if meta_active and hasattr(db_manager, "get_genre_affinity"):
        try:
            genre_model = await db_manager.get_genre_affinity()
        except Exception:
            genre_model = {}

    async def _ensure_meta(paths: list[str]) -> None:
        nonlocal meta_active
        if not meta_active:
            return
        missing = [p for p in paths if p not in meta_map]
        if not missing:
            return
        try:
            meta_map.update(await db_manager.get_artist_meta_for_paths(missing))
        except Exception:
            meta_active = False

    # ── Scan the ranking in blocks until the queue fills ─────────────────────
    # Bounds the enrichment fetch: a queue of 10 almost always fills inside the
    # first block, so a 20K-track library costs the same two queries as a 1K one.
    selected: list[str] = []
    artist_hits: dict[str, int] = {}
    album_hits: dict[str, int] = {}
    gate_on = meta_active and veto_genre_floor > 0.0
    block = max(200, 20 * length)

    for start in range(0, len(ranked), block):
        if len(selected) >= length:
            break
        chunk = ranked[start:start + block]
        if gate_on:
            await _ensure_meta([seed_path, *chunk])
        for path in chunk:
            if len(selected) >= length:
                break
            if gate_on and _pool_foreign(
                seed_path, path, meta_map, genre_model, veto_genre_floor,
            ):
                continue
            row = display.get(path) or {}
            # Artist cap, keyed on credit-string membership so a collab credit
            # counts against the artist it names.
            keys = _credit_key_set(row.get("artist"))
            if max_per_artist > 0 and keys and any(
                artist_hits.get(k, 0) >= max_per_artist for k in keys
            ):
                continue
            album = row.get("album")
            if max_per_album > 0 and album and album_hits.get(album, 0) >= max_per_album:
                continue
            selected.append(path)
            for k in keys:
                artist_hits[k] = artist_hits.get(k, 0) + 1
            if album:
                album_hits[album] = album_hits.get(album, 0) + 1

    return selected


async def bulk_analyze_library(
    db_manager,
    audio_service,
    progress_cb=None,
    cancel_check=None,
    features_version: int = FEATURES_VERSION,
) -> dict:
    """Analyse every track lacking current-version features and persist the
    extracted descriptors.

    Per-track cost on a modern phone is ~3–6 s (90 s decode @ hardware codec
    + numpy DSP). For a 5K-track fresh library this is ~5 h of CPU — caller
    is responsible for surfacing that to the user. `progress_cb(done, total,
    current_path, failures)` is invoked after every track, sync or async.
    `cancel_check()` is polled before each track so the caller can abort
    early without leaving an orphaned analyser running.

    Returns {analysed, failed, total} counts.
    """
    missing = await db_manager.get_tracks_missing_features(features_version)
    total = len(missing)
    if total == 0:
        return {"analysed": 0, "failed": 0, "total": 0}

    analysed = 0
    failures = 0

    async def _emit(done: int, current: str) -> None:
        if not progress_cb:
            return
        try:
            res = progress_cb(done, total, current, failures)
            if hasattr(res, "__await__"):
                await res
        except Exception as ex:
            logger.warning("track_graph: progress_cb raised %s", ex)

    for i, path in enumerate(missing, 1):
        if cancel_check is not None:
            try:
                if cancel_check():
                    break
            except Exception:
                pass

        try:
            features = await analyze_track(audio_service, path)
        except Exception as ex:
            failures += 1
            logger.warning("track_graph: analyse failed for %s: %s", path, ex)
            await _emit(i, path)
            continue

        try:
            await db_manager.update_track_features(
                path,
                features.bpm, features.energy, features.brightness,
                features.rolloff, features.beat_strength,
                features.spectral_flatness, features.spectral_contrast,
                features.key_index,
                features.timbre_blob(), features_version,
            )
            analysed += 1
        except Exception as ex:
            failures += 1
            logger.warning("track_graph: persist failed for %s: %s", path, ex)

        await _emit(i, path)

    return {"analysed": analysed, "failed": failures, "total": total}


async def graph_status(db_manager) -> dict:
    """Compact summary used by the assistant's first-open initialisation flow,
    the startup readiness check, and the 'graph health' Settings panel.

    `coord_tracks` is THE acoustic-readiness field: the walk loads persisted Zr
    coordinates, so that column — not `track_neighbors` — says whether the
    similarity graph exists. `acoustic_edges` is retained only so older callers
    don't KeyError; it is structurally 0 now that `build_acoustic_edges`
    persists geometry instead of edge rows, and must not be used to decide
    whether to build (doing so is what left a real device with 1149 analysed
    tracks, no coordinates, and a walk that returned nothing for every seed)."""
    total_tracks = await db_manager.get_total_tracks()
    coord_tracks = 0
    if hasattr(db_manager, "count_tracks_with_coords"):
        coord_tracks = await db_manager.count_tracks_with_coords()
    return {
        "total_tracks": total_tracks,
        "coord_tracks":   coord_tracks,
        "acoustic_edges": await db_manager.count_neighbors(KIND_ACOUSTIC),
        "artist_edges":   await db_manager.count_neighbors(KIND_ARTIST),
        "album_edges":    await db_manager.count_neighbors(KIND_ALBUM),
    }
