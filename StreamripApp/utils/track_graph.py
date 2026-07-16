"""
Track graph: sparse k-NN adjacency over the music library.

Two tiers of edges:

  • acoustic  — Euclidean k-NN over the z-scored, PCA-reduced DSP feature
                vectors persisted by dsp.analyze_track(), reweighted by a
                Zelnik-Manor self-tuning Gaussian kernel. Top-K-per-source
                candidates are pruned by strict mutual-kNN intersection (keep
                edge i→j iff j ∈ topK(i) AND i ∈ topK(j)) so cluster-centroid
                "hub" tracks don't dominate every walk. The same affinity
                graph is the substrate for Louvain community detection
                (cluster_id), so walk and clustering share one geometry.
  • metadata  — same-artist and same-album co-occurrence (edge_kind 'artist'
                / 'album'). Weight is fixed at 1.0; ordering inside a tier
                falls back to library order.

The graph is the navigation backbone for the assistant: it routes 'play
something similar' to a seed-anchored trajectory walk over the acoustic graph
(see `walk`), and 'more by this artist' to artist neighbours. Provides the
continuous proximity the assistant needs instead of discrete buckets.

The walk (`walk`, "Seed-Anchored Smooth Flow"):
  • deterministic greedy trajectory — at each step pick the unvisited acoustic
    neighbour maximising 0.7·Sim(current) + 0.3·Sim(seed), so the queue flows
    forward while staying anchored to the seed (no random teleports);
  • a multiplicative metadata factor (genre-NPMI + shared-country, `_meta_score`)
    and a soft cross-community penalty, so the trajectory stays inside the seed's
    genre/community instead of riding a timbre bridge into a foreign one;
  • graceful degradation — with no enrichment / cluster labels the factors
    collapse to 1.0 and it's the pure acoustic dual-similarity flow.

(A stochastic Personalised-PageRank walker previously lived here; it was removed
in favour of this single, metadata-aware walker.)

All builders are async (DB-bound) but the numpy work runs synchronously —
no off-thread call is necessary for libraries up to ~20K tracks; Euclidean kNN
over the PCA-reduced vectors is bandwidth-limited and finishes in under a second.
For very large libraries the caller should wrap build_acoustic_edges in
asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import numpy as np

from utils.dsp import (
    GRAPH_EMBED_DIMS,
    FEATURES_VERSION,
    analyze_track,
    unpack_graph_embedding,
)
from utils.harmonic import key_index_to_camelot

logger = logging.getLogger(__name__)

# ── Coordinate-graph cache ───────────────────────────────────────────────────
# The walk's similarity oracle is the coordinate graph (persisted Zr coords +
# self-tuning σ/threshold matrices). Building it does three O(N²) passes
# (load_live_coordinate_graph); without a cache that ran on *every* walk() call
# — i.e. every Play-Similar press recomputed global bandwidths for the whole
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


# Top-K acoustic neighbours stored per track. 20 is enough for both 'most
# similar' lookups and short random walks; bigger K eats DB rows without
# adding signal.
DEFAULT_K_ACOUSTIC = 20
# Top-K metadata neighbours stored per track per kind. Albums rarely have
# more than ~15 tracks; artists can have hundreds, but the assistant only
# samples a handful at a time.
DEFAULT_K_METADATA = 30

# Edge-kind tags written into track_neighbors.edge_kind.
KIND_ACOUSTIC = "acoustic"
KIND_ARTIST = "artist"
KIND_ALBUM = "album"


# ── Builders ─────────────────────────────────────────────────────────────────

# ── Builders ─────────────────────────────────────────────────────────────────


# Canonical order of the scalar descriptors appended to the timbre block
# (EMBED_DIMS floats: mfcc mean/std/delta + chroma + rhythm, see dsp.py).
# `bpm` denotes the log2(bpm) column; cos_h/sin_h are the Camelot unit-circle
# coords (structural — never cleaved by the covariance analysis).
_SCALAR_ORDER = (
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast", "cos_h", "sin_h", "key_mode",
)
_STRUCTURAL_SCALARS = frozenset({"cos_h", "sin_h", "key_mode"})
# Harmonic columns excluded from SVD and late-fused after PCA projection.
# Their rigid Camelot-wheel geometry must not be rotated by PCA.
_HARMONIC_SCALARS = frozenset({"cos_h", "sin_h", "key_mode"})


def _all_scalars(row: dict) -> dict[str, float]:
    """Every scalar descriptor for one track row, keyed by `_SCALAR_ORDER`.
    `bpm` is returned as log2(bpm); the Camelot key is encoded as
    (cos_h, sin_h, key_mode)."""
    bpm_raw = float(row.get("bpm", 0) or 0)
    log_bpm = float(np.log2(max(bpm_raw, 1.0)))
    ki = row.get("key_index", 0) or 0
    cam = key_index_to_camelot(ki)
    if cam is None:
        cos_h, sin_h, key_mode = 0.0, 0.0, 0.0
    else:
        hour, ring = cam
        theta = 2.0 * np.pi * (hour - 1) / 12.0
        cos_h = float(np.cos(theta))
        sin_h = float(np.sin(theta))
        key_mode = 1.0 if ring == "B" else 0.0
    return {
        "bpm": log_bpm,
        "brightness": float(row.get("brightness", 0) or 0),
        "energy": float(row.get("energy", 0) or 0),
        "rolloff": float(row.get("rolloff", 0) or 0),
        "beat_strength": float(row.get("beat_strength", 0) or 0),
        "spectral_flatness": float(row.get("spectral_flatness", 0) or 0),
        "spectral_contrast": float(row.get("spectral_contrast", 0) or 0),
        "cos_h": cos_h,
        "sin_h": sin_h,
        "key_mode": key_mode,
    }


def _surviving_scalars(redundant: set[str]) -> list[str]:
    """Scalar names that survive covariance cleaving, in canonical order.
    Structural/harmonic coords (cos_h/sin_h/key_mode) are always kept."""
    return [
        s for s in _SCALAR_ORDER
        if s in _STRUCTURAL_SCALARS or s not in redundant
    ]


def _feature_vector(row: dict, timbre: np.ndarray, surviving: list[str]) -> np.ndarray:
    """Full graph feature vector for one track: the EMBED_DIMS timbre block
    followed by the surviving scalar descriptors in `surviving` order."""
    sc = _all_scalars(row)
    scalars = np.array([sc[s] for s in surviving], dtype=np.float32)
    return np.concatenate([timbre.astype(np.float32), scalars])
async def build_acoustic_edges(
    db_manager,
    k: int = DEFAULT_K_ACOUSTIC,
    features_version: int = FEATURES_VERSION,
    z_score: bool = True,
    scalar_weight: float = 1.5,
    harmonic_weight: float = 1.5,
) -> int:
    """Recompute the acoustic tier of the graph from scratch.

    Loads every track that has a current-version feature BLOB, optionally
    z-scores the vectors (or centers them if z_score=False), and writes the
    top-K neighbours per track back to `track_neighbors`. Returns the edge
    count written.

    Coverage degrades gracefully: tracks without features are simply absent
    from the acoustic graph. The assistant falls back to metadata edges for
    those.
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
    # The graph — and the Louvain communities + similarity walk built on it —
    # uses every feature that survives the unsupervised PCA / Pearson-covariance
    # analysis. Collinear scalars (e.g. rolloff ↔ brightness) are cleaved so
    # they don't double-count toward distance. The 68-D graph embedding timbre block and the
    # harmonic unit-circle coords (cos_h/sin_h) are structural and always kept;
    # only the raw scalar descriptors are subject to cleaving.
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
    for r in rows:
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

    if len(vectors) < 2:
        return 0

    X = np.stack(vectors, axis=0)  # (N, EMBED_DIMS + len(surviving))

    # ── Late Fusion split: separate harmonic columns from continuous ──────
    # The harmonic unit-circle coords (cos_h, sin_h, key_mode) encode the
    # rigid Camelot wheel geometry. SVD rotates *all* axes into mixed PCs,
    # which destroys that geometric integrity. Late Fusion keeps them out of
    # the PCA entirely: the SVD denoises only the ~75-D timbre+dynamics, and
    # the raw harmonic coordinates are concatenated back after projection.
    harmonic_names_in_surviving = [s for s in surviving if s in _HARMONIC_SCALARS]
    harm_col_indices = []   # column indices in X that are harmonic
    cont_col_indices = list(range(GRAPH_EMBED_DIMS))  # timbre block is always continuous
    for i, s in enumerate(surviving):
        col = GRAPH_EMBED_DIMS + i
        if s in _HARMONIC_SCALARS:
            harm_col_indices.append(col)
        else:
            cont_col_indices.append(col)

    X_cont = X[:, cont_col_indices]   # (N, D_cont)
    X_harm = X[:, harm_col_indices]   # (N, n_harm)  — typically 3

    # z-score the continuous block.
    mu_cont = X_cont.mean(axis=0)
    if z_score:
        sd_cont = X_cont.std(axis=0)
        sd_cont = np.where(sd_cont < 1e-8, 1.0, sd_cont)
    else:
        sd_cont = np.ones(X_cont.shape[1], dtype=X_cont.dtype)
    Z_cont = (X_cont - mu_cont) / sd_cont

    # z-score the harmonic block separately (preserves circle geometry).
    mu_harm = X_harm.mean(axis=0)
    if z_score:
        sd_harm = X_harm.std(axis=0)
        sd_harm = np.where(sd_harm < 1e-8, 1.0, sd_harm)
    else:
        sd_harm = np.ones(X_harm.shape[1], dtype=X_harm.dtype)
    Z_harm = (X_harm - mu_harm) / sd_harm

    # Boost the non-timbre continuous scalars AFTER z-scoring so tempo/dynamics
    # carry weight comparable to the individual timbre axes.
    n_cont_scalars = Z_cont.shape[1] - GRAPH_EMBED_DIMS
    if scalar_weight != 1.0 and n_cont_scalars > 0:
        Z_cont[:, GRAPH_EMBED_DIMS:] *= scalar_weight

    # ── PCA reduction (Kaiser-truncated SVD on the *continuous* matrix) ─────
    # Only the timbre + continuous dynamics enter the SVD; the harmonic columns
    # are fused back afterwards. This ensures the Camelot wheel's cos/sin
    # geometry is 100% preserved in the final affinity calculation.
    Zr_cont = Z_cont.astype(np.float32)
    N = Z_cont.shape[0]
    V_keep = np.eye(Z_cont.shape[1], dtype=np.float32)
    eigenvalues = np.zeros(Z_cont.shape[1], dtype=np.float32)
    if Z_cont.shape[1] >= 4 and N > 1:
        # SVD is the single heaviest op in the build; run it off the loop
        # (LAPACK releases the GIL) so it can't freeze the UI.
        _U, _S, _Vt = await asyncio.to_thread(
            np.linalg.svd, Z_cont, full_matrices=False,
        )
        eigenvalues = (_S ** 2) / float(N - 1)
        kaiser_k = int((eigenvalues > 1.0).sum())
        kaiser_k = max(3, min(kaiser_k, _Vt.shape[0]))
        V_keep = _Vt[:kaiser_k].T.astype(np.float32)     # (D_cont, kaiser_k)
        Zr_cont = (Z_cont @ V_keep).astype(np.float32)   # (N, kaiser_k)
        cum_var = float(eigenvalues[:kaiser_k].sum() / eigenvalues.sum()) if eigenvalues.sum() > 0 else 0.0
        logger.info(
            "track_graph: PCA-reduced continuous dims from %d to %d "
            "(Kaiser λ>1; %.1f%% variance retained)",
            int(_Vt.shape[1]), V_keep.shape[1], cum_var * 100.0,
        )

    # ── Late Fusion: concatenate the raw harmonic coords onto Zr ───────────
    H_fused = (Z_harm * harmonic_weight).astype(np.float32)  # (N, n_harm)
    Zr = np.concatenate([Zr_cont, H_fused], axis=1)          # (N, kaiser_k + n_harm)
    logger.info(
        "track_graph: late-fused %d harmonic dims (weight=%.2f) → "
        "final Zr %d-D",
        H_fused.shape[1], harmonic_weight, Zr.shape[1],
    )

    # ── Persist the unified geometry (projection + per-track Zr coords) ────
    # Single source of the graph's Zr space: any on-demand
    # projection of new tracks reads it back via load_pca_space() /
    # project_to_zr(). The stored means/stds correspond to the *continuous*
    # columns only; harmonic stats are stored separately in feature_spec so
    # project_to_zr can replicate the same late-fusion split.
    if hasattr(db_manager, "save_pca_space"):
        try:
            feature_spec = {
                "surviving": surviving,
                "scalar_weight": float(scalar_weight),
                "embed_dims": int(GRAPH_EMBED_DIMS),
                "z_score": bool(z_score),
                "harmonic_names": harmonic_names_in_surviving,
                "harmonic_weight": float(harmonic_weight),
                "harmonic_means": mu_harm.astype(np.float32).tolist(),
                "harmonic_stds": sd_harm.astype(np.float32).tolist(),
                "k_neighbors": int(k),
            }
            await db_manager.save_pca_space(
                mu_cont.astype(np.float32), sd_cont.astype(np.float32),
                V_keep, eigenvalues.astype(np.float32), feature_spec,
            )
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
    for paths in by_artist.values():
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

def _unpack_embedding(blob: bytes | None) -> Optional[np.ndarray]:
    """Unpack a timbre BLOB to an L2-normalised float32 vector suitable for
    cosine on the graph timbre sub-space (mfcc mean/std + chroma + rhythm; delta
    excluded, matching the geometry). Returns None when the blob is absent or
    malformed. Cosine helper for embedding-space scoring (e.g. a negative-taste
    centroid); callers fetch BLOBs via db_manager.get_embeddings_for_paths."""
    v = unpack_graph_embedding(blob)
    if v is None:
        return None
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return None
    return (v / n).astype(np.float32)


# ── Regional scenes (the ONE genre-taxonomy fact the walk still needs) ────────
# A regional scene travels with a country/language, so for a seed in one of
# these buckets a foreign country IS foreign — even a moderate cross-country
# genre overlap (laiko↔dance-pop via NPMI) is the wrong continuation, and the
# right one (another same-country track) is often *untagged*, so genre-set
# similarity can't rank it. `_pool_foreign` therefore treats country as a HARD
# pool constraint for regional seeds. Everything else (Hip-Hop, Rock, Pop,
# Metal, Electronic, Jazz, …) is a borderless/international scene where
# nationality says little, so country there is only the soft ordering bonus in
# `_meta_score`.
_REGIONAL_SCENES = frozenset({"Folk/Cntry", "Latin", "Reggae", "Asian-Pop"})
# Flat weight of a shared artist-country in the `_meta_score` ordering bonus.
# One constant (was a 3-tier γ keyed on genre_bucket): the regional/borderless
# distinction now lives entirely in the pool constraint, not the score.
_COUNTRY_W = 0.15


def _is_regional(genres) -> bool:
    """True iff any of a seed's genres falls in a regional-scene bucket."""
    if not genres:
        return False
    from utils.pca_engine import genre_bucket
    return any(genre_bucket(g) in _REGIONAL_SCENES for g in genres)


def _meta_score(a_path: str, b_path: str, meta_map: dict, genre_model: dict) -> float:
    """Metadata affinity between two tracks: soft genre-set similarity (NPMI
    'genre-BLOSUM') plus a flat ADDITIVE shared-country bonus  gx + _COUNTRY_W·same_cty.

    Additive (not the old gx·(1+β·same_cty)) so shared provenance contributes
    EVEN WHEN genre overlap is thin. Returns 0 when either track lacks
    enrichment, or when they're the same artist (that coherence is already
    carried by the artist edge tier). Used by `walk` to fold provenance/genre
    proximity into the acoustic ordering.
    """
    from utils.genre_similarity import soft_set_sim
    ma = meta_map.get(a_path)
    mb = meta_map.get(b_path)
    if not ma or not mb:
        return 0.0
    aa, ab = ma.get("artist"), mb.get("artist")
    if aa and aa == ab:
        return 0.0  # same-artist coherence already carried by the artist edge tier

    ca, cb = ma.get("country"), mb.get("country")
    same_cty = 1.0 if (ca and ca == cb) else 0.0
    ga = ma.get("genres") or frozenset()
    gb = mb.get("genres") or frozenset()
    gx = soft_set_sim(ga, gb, genre_model)
    return gx + _COUNTRY_W * same_cty


def _genre_flow(a_path: str, b_path: str, meta_map: dict, genre_model: dict) -> float:
    """NPMI soft-set genre similarity between two tracks, genre-only (no country,
    no same-artist zeroing). Powers the within-pool *genre-continuity* gradient:
    the walk's `_meta_score` bonus is anchored to the SEED, so it rewards genre
    closeness to where you started; this is anchored to the CURRENT track, so it
    rewards a smooth step-to-step genre trajectory *inside* the pool (grime →
    grime → grime before broadening to trap), never changing pool membership.
    Orthogonal to the acoustic flow term by design — it carries the tag signal
    the timbre metric can't see. Returns 0 when either track lacks tags."""
    from utils.genre_similarity import soft_set_sim
    ma = meta_map.get(a_path)
    mb = meta_map.get(b_path)
    if not ma or not mb:
        return 0.0
    ga = ma.get("genres") or frozenset()
    gb = mb.get("genres") or frozenset()
    if not ga or not gb:
        return 0.0
    return soft_set_sim(ga, gb, genre_model)


def _pool_foreign(
    seed_path: str, cand_path: str, meta_map: dict, genre_model: dict, floor: float,
) -> bool:
    """Pool membership test: True iff `cand` is *known* to be foreign to the
    seed and must be excluded from the walk's candidate pool outright — a strong
    timbre bridge must never carry the queue across this boundary.

    Two evidence-gated boundaries, either one foreign-marks the candidate:
      • GENRE — both carry genre tokens and their NPMI soft-set similarity is
        below `floor` (the Carti→laiko timbre-bridge across an obvious genre gap).
      • COUNTRY (regional scenes only) — the SEED is a regional scene
        (`_is_regional`) and both tracks carry a country and the countries
        differ. For laiko/Latin/Reggae/Asian-Pop, cross-country IS the genre
        jump, and it catches the case genre can't: the right same-country
        continuation is frequently untagged, so only country separates it from
        an acoustically-near foreign-pop track.

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
    if ms.get("artist") and ms.get("artist") == mc.get("artist"):
        return False  # same artist is never foreign
    gs = ms.get("genres") or frozenset()
    gc = mc.get("genres") or frozenset()
    # Genre boundary (needs tags on both sides).
    if gs and gc and soft_set_sim(gs, gc, genre_model) < floor:
        return True
    # Country boundary for regional seeds (needs a country on both sides).
    cs, cc = ms.get("country"), mc.get("country")
    if cs and cc and cs != cc and _is_regional(gs):
        return True
    return False


async def load_live_coordinate_graph(db_manager):
    """Load all PCA coordinates, cluster IDs, and metadata from SQLite to build
    a coordinates-only graph representation in RAM for on-the-fly walk traversal.
    """
    rows = await db_manager.get_tracks_pca_coords()
    if not rows:
        return None

    # Load space projection parameters for K-neighbors.
    proj = await db_manager.load_pca_space()
    k_neighbors = 50
    if proj:
        k_neighbors = proj.get("k_neighbors", 50)

    paths = [r["path"] for r in rows if r.get("pca_coords") is not None]
    if not paths:
        return None

    path_to_idx = {p: i for i, p in enumerate(paths)}
    X_zr = np.array([r["pca_coords"] for r in rows if r.get("pca_coords") is not None], dtype=np.float32)

    # meta_map for genre/country checks
    meta_map = {r["path"]: r for r in rows}

    def _compute_graph_matrices():
        N = X_zr.shape[0]
        k_eff = min(k_neighbors, N - 1)
        X_zr_sq = np.sum(X_zr ** 2, axis=1)

        # 1. Compute sigmas
        LOCAL_K = 7
        chunk = 1024
        sigmas = np.ones(N, dtype=np.float32)
        for i in range(0, N, chunk):
            block = X_zr[i:i + chunk]
            c = block.shape[0]
            d2 = X_zr_sq[i:i + c, None] - 2.0 * (block @ X_zr.T) + X_zr_sq[None, :]
            for j in range(c):
                d2[j, i + j] = np.inf
            piv = np.partition(d2, LOCAL_K - 1, axis=1)[:, LOCAL_K - 1]
            sigmas[i:i + c] = np.sqrt(np.maximum(piv, 0.0))
        sigmas = np.maximum(sigmas, 1e-3)

        # 2. Compute top-K affinity thresholds (for mutual-kNN membership).
        thresholds = np.zeros(N, dtype=np.float32)
        for i in range(0, N, chunk):
            block = X_zr[i:i + chunk]
            c = block.shape[0]
            d2 = X_zr_sq[i:i + c, None] - 2.0 * (block @ X_zr.T) + X_zr_sq[None, :]
            for j in range(c):
                d2[j, i + j] = np.inf
            A = np.exp(-d2 / (sigmas[i:i + c, None] * sigmas[None, :]))
            piv_sel = np.partition(A, N - k_eff, axis=1)[:, N - k_eff]
            thresholds[i:i + c] = piv_sel

        return sigmas, thresholds, X_zr_sq, k_eff

    sigmas, thresholds, X_zr_sq, k_eff = await asyncio.to_thread(_compute_graph_matrices)

    return {
        "X_zr": X_zr,
        "X_zr_sq": X_zr_sq,
        "paths": paths,
        "path_to_idx": path_to_idx,
        "meta_map": meta_map,
        "sigmas": sigmas,
        "thresholds": thresholds,
        "k_eff": k_eff,
    }


async def walk(
    db_manager,
    seed_path: str,
    length: int = 10,
    avoid: Optional[set[str]] = None,
    meta_lambda: float = 0.35,
    genre_flow_lambda: float = 0.0,
    mmr_lambda: float = 0.0,
    temperature: float = 0.0,
    rng_seed: int | None = None,
    veto_genre_floor: float = 0.06,
) -> list[str]:
    """Seed-anchored trajectory walk over the track graph.

    This is *the* walk. The candidate POOL is defined by metadata and only then
    ORDERED by acoustics — the inversion that replaced a stack of acoustic
    correction terms (cross-cluster penalty, multiplicative genre nudge) with one
    membership rule:

        pool(Seed)  = unvisited acoustic neighbours of T_i that are NOT
                      `_pool_foreign` to the Seed (genre boundary, or country
                      boundary for regional scenes)
        Score(C)    = 0.7·Sim(T_i, C) + 0.3·Sim(Seed, C)   # acoustic (dual)
                    + meta_lambda·meta(Seed, C)            # genre/country bonus (seed-anchored)
                    + genre_flow_lambda·gx(T_i, C)         # genre continuity (current-anchored)

    Why a metadata pool instead of acoustic corrections: a timbre bridge puts a
    laiko ballad next to a trap track (same acoustic neighbourhood, even the same
    Louvain community), so no acoustic penalty can be trusted to stop the jump —
    only the tags reveal it. `_pool_foreign` is that categorical gate, and for a
    regional-scene seed it also treats a foreign country as foreign (the coherent
    same-country continuation is often untagged, so genre similarity alone can't
    rank it above an acoustically-near foreign-pop track). Within the pool the
    acoustics do what they're good at: order by proximity, anchored to the seed
    for both the flow term Sim(T_i,·) and the tether Sim(Seed,·).

    Everything degrades gracefully: with no coordinate graph the anchor falls
    back to the stored top-K; with no enrichment nothing is `_pool_foreign` and
    the meta term is 0 (evidence-gated), so the walk is the pure acoustic
    dual-similarity flow — exactly what the test fakes exercise.

    Optional refinements — all off / deterministic by default, so the contract
    above is unchanged unless a caller opts in:
      • genre_flow_lambda>0 rewards NPMI genre continuity to the CURRENT track
        (`_genre_flow`), so the queue prefers a smooth step-to-step genre
        trajectory *inside* the pool (grime → grime → grime before broadening to
        trap) instead of subgenre pinball. Never changes pool membership — the
        veto still fences the genre; this only shapes the path within it.
      • mmr_lambda>0 adds a Maximal-Marginal-Relevance diversity penalty: a
        candidate is discounted by its timbre cosine to the tracks already
        emitted, so the queue stops chaining remixes / alternate mixes / the
        same song on another release.
      • temperature>0 samples among the top candidates (scale-invariant
        score**(1/T) weights) instead of taking the arg-max, so repeated walks
        from one seed vary rather than returning an identical queue; T->0 is the
        arg-max. rng_seed makes a stochastic walk reproducible.
    """
    visited: set[str] = set(avoid or set())
    visited.add(seed_path)
    path_seq: list[str] = []

    # The coordinate graph is the walk's similarity oracle: it lets us compute
    # the seed-anchor affinity for ANY candidate (not just the seed's stored
    # top-K), which is what keeps the queue tethered to the seed. It's cached
    # across walks (build invalidates it). A backend without coordinates (the
    # test fakes, or a library that hasn't been built) returns None and the walk
    # falls back to the persisted edge table + the top-K seed_sim_map.
    coord_graph = None
    try:
        coord_graph = await _coord_graph_cached(db_manager)
    except Exception as exc:
        logger.warning("track_graph.walk: no coordinate graph, using edge table: %s", exc)

    def _get_live_neighbors(path: str, k: int = 40) -> list[dict]:
        if coord_graph is None:
            return []
        src_idx = coord_graph["path_to_idx"].get(path)
        if src_idx is None:
            return []
        X_zr = coord_graph["X_zr"]
        X_zr_sq = coord_graph["X_zr_sq"]
        sigmas = coord_graph["sigmas"]
        thresholds = coord_graph["thresholds"]
        paths = coord_graph["paths"]
        d2 = X_zr_sq[src_idx] - 2.0 * (X_zr[src_idx] @ X_zr.T) + X_zr_sq
        d2[src_idx] = np.inf
        A = np.exp(-d2 / (sigmas[src_idx] * sigmas))
        mutual_mask = (A >= np.maximum(thresholds[src_idx], thresholds) - 1e-5)
        mutual_mask[src_idx] = False
        nbr_indices = np.where(mutual_mask)[0]
        if len(nbr_indices) == 0:
            return []
        nbr_affinities = A[nbr_indices]
        sort_order = np.argsort(-nbr_affinities)
        sorted_indices = nbr_indices[sort_order]
        res = []
        for idx in sorted_indices:
            p = paths[idx]
            m = coord_graph["meta_map"].get(p, {})
            res.append({
                "path": p,
                "weight": float(A[idx]),
                "edge_kind": KIND_ACOUSTIC,
                "title": m.get("title"),
                "artist": m.get("artist"),
                "album": m.get("album"),
            })
        return res[:k]

    # ── Neighbour access: one batched prefetch, then an in-memory cache ──────
    # The walk is greedy (each step depends on the previous choice), but the
    # early steps almost always land inside the seed's own neighbourhood. We
    # warm a cache with the seed's neighbours' acoustic neighbours in ONE
    # batched query and serve per-step lookups from it, collapsing the old
    # O(length) sequential round-trips to ~2. A cache miss (a step that wandered
    # past the prefetched horizon) does a single live fetch; a backend without
    # the batch accessor (the test fakes) just uses live fetches throughout.
    # Either way the rows — and therefore the walk's output — are identical.
    async def _neighbors_of(path: str) -> list[dict]:
        if coord_graph is not None:
            return _get_live_neighbors(path, k=40)
        # Test-support fallback when coord_graph is None:
        if hasattr(db_manager, "get_neighbors_multi"):
            return await db_manager.get_neighbors_multi(path, (KIND_ACOUSTIC,), k=40)
        if hasattr(db_manager, "get_neighbors"):
            return await db_manager.get_neighbors(path, k=40, edge_kind=KIND_ACOUSTIC)
        return []

    # Seed's acoustic neighbours (k=50) drive the seed-anchor term and the
    # dead-end fallback.
    if coord_graph is not None:
        seed_nbrs = _get_live_neighbors(seed_path, k=50)
    else:
        # Test-support fallback when coord_graph is None:
        if hasattr(db_manager, "get_neighbors_multi"):
            seed_nbrs = await db_manager.get_neighbors_multi(seed_path, (KIND_ACOUSTIC,), k=50)
        elif hasattr(db_manager, "get_neighbors"):
            seed_nbrs = await db_manager.get_neighbors(seed_path, k=50, edge_kind=KIND_ACOUSTIC)
        else:
            seed_nbrs = []

    seed_sim_map: dict[str, float] = {
        n["path"]: float(n.get("weight", 0.5)) for n in seed_nbrs if n.get("path")
    }

    # ── Seed-anchor affinity for ANY candidate (not just the seed's top-K) ────
    # The fix for anchor evaporation: the old walk read the 0.3·seed term from
    # seed_sim_map, which only held the seed's top-50 neighbours and defaulted to
    # 0.0 beyond them — so the moment the greedy walk stepped outside that
    # neighbourhood the anchor silently vanished and the queue drifted off-seed.
    # With the coordinate graph we compute the self-tuning affinity
    # exp(-d²/(σ_seed·σ_c)) against the seed for every candidate, so the tether
    # never dies. Falls back to the top-K seed_sim_map with no coord graph.
    _seed_idx = coord_graph["path_to_idx"].get(seed_path) if coord_graph else None
    _seed_d2 = None
    if coord_graph is not None and _seed_idx is not None:
        _X = coord_graph["X_zr"]
        _Xsq = coord_graph["X_zr_sq"]
        _seed_d2 = _Xsq[_seed_idx] - 2.0 * (_X[_seed_idx] @ _X.T) + _Xsq

    def _seed_affinity(path: str) -> float:
        if _seed_d2 is not None:
            j = coord_graph["path_to_idx"].get(path)
            if j is not None:
                sig = coord_graph["sigmas"]
                return float(np.exp(-_seed_d2[j] / (sig[_seed_idx] * sig[j])))
        return seed_sim_map.get(path, 0.0)

    # ── Metadata context (genre-NPMI + country) ──────────────────────────
    # meta_map is filled lazily as candidates are seen; the genre model loads
    # once. If the backend can't serve enrichment, meta_active latches off and
    # the metadata factor is 1.0 for the rest of the walk.
    meta_active = meta_lambda > 0.0 and hasattr(db_manager, "get_artist_meta_for_paths")
    meta_map: dict[str, dict] = {}
    genre_model: dict = {}
    # NB: meta_map is filled ONLY from get_artist_meta_for_paths (the
    # {artist, country, genres} shape _meta_score/the veto need). We do NOT seed
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

    # ── Diversity context (MMR) ───────────────────────────────────────────
    # Penalise a candidate that is near-identical (high timbre cosine) to a
    # track already emitted, so the queue stops chaining remixes / alternate
    # mixes / the same song on another release. Uses the persisted graph
    # embeddings; degrades to off when mmr_lambda == 0 or the backend can't
    # serve embeddings (e.g. the test fakes).
    mmr_active = mmr_lambda > 0.0 and hasattr(db_manager, "get_embeddings_for_paths")
    emb_cache: dict[str, np.ndarray] = {}
    selected_embs: list[np.ndarray] = []

    async def _ensure_emb(paths: list[str]) -> None:
        nonlocal mmr_active
        if not mmr_active:
            return
        missing = [p for p in paths if p not in emb_cache]
        if not missing:
            return
        try:
            blobs = await db_manager.get_embeddings_for_paths(missing)
        except Exception:
            mmr_active = False
            return
        for p in missing:
            v = _unpack_embedding(blobs.get(p))
            if v is not None:
                emb_cache[p] = v

    await _ensure_meta([seed_path, *seed_sim_map.keys()])

    rng = random.Random(rng_seed) if temperature > 0.0 else None
    _TEMP_TOP_N = 6

    current = seed_path
    for _step in range(length):
        raw_nbrs = await _neighbors_of(current)
        candidates = [n for n in raw_nbrs if n.get("path") and n["path"] not in visited]

        # Fallback to seed's acoustic neighbors if current node hits a dead end
        if not candidates and current != seed_path:
            candidates = [n for n in seed_nbrs if n.get("path") and n["path"] not in visited]

        if not candidates:
            break

        await _ensure_meta([c["path"] for c in candidates])

        # ── Metadata pool (anchored to the seed) ──────────────────────────────
        # Restrict the pool to candidates that are NOT `_pool_foreign` to the
        # seed: a strong timbre bridge can never carry the queue across a genre
        # boundary (Carti→laiko), nor — for a regional-scene seed — across a
        # country boundary. Evidence-gated, so unenriched candidates survive.
        if meta_active and veto_genre_floor > 0.0:
            kept = [
                c for c in candidates
                if not _pool_foreign(
                    seed_path, c["path"], meta_map, genre_model, veto_genre_floor,
                )
            ]
            if not kept:
                # Every neighbour of `current` is foreign to the seed. Do NOT
                # admit a foreign track (the old behaviour silently broke the
                # guarantee). RE-ANCHOR to the seed's own unvisited, in-pool
                # neighbours and continue from the seed's vicinity instead. Their
                # meta is already loaded (see the _ensure_meta on seed_sim_map).
                kept = [
                    n for n in seed_nbrs
                    if n.get("path") and n["path"] not in visited
                    and not _pool_foreign(
                        seed_path, n["path"], meta_map, genre_model, veto_genre_floor,
                    )
                ]
            if not kept:
                # Nothing in-genre is reachable anywhere — end the queue rather
                # than step foreign. Lowering veto_genre_floor is the escape
                # valve for callers who prefer length over strict purity.
                break
            candidates = kept

        if mmr_active:
            await _ensure_emb([c["path"] for c in candidates])

        # Order the pool: additive acoustic-dual + seed-anchored metadata bonus
        # + (opt) current-anchored genre-continuity, then the MMR-diversity haircut.
        scored: list[tuple[float, dict]] = []
        for c in candidates:
            w_curr = max(0.0, float(c.get("weight", 0.5)))
            w_seed = _seed_affinity(c["path"])
            score = 0.7 * w_curr + 0.3 * w_seed
            if meta_active:
                score += meta_lambda * _meta_score(
                    seed_path, c["path"], meta_map, genre_model,
                )
                if genre_flow_lambda > 0.0:
                    score += genre_flow_lambda * _genre_flow(
                        current, c["path"], meta_map, genre_model,
                    )
            if mmr_active and selected_embs:
                ev = emb_cache.get(c["path"])
                if ev is not None:
                    sim = max(float(ev @ s) for s in selected_embs)
                    score *= 1.0 - mmr_lambda * min(1.0, max(0.0, sim))
            scored.append((score, c))

        # Selection: deterministic arg-max by default; when temperature > 0,
        # sample among the top candidates (scale-invariant score**(1/T) weights)
        # so repeated walks from one seed vary. T -> 0 reproduces the arg-max.
        best_cand = None
        if rng is not None and len(scored) > 1:
            ranked = sorted(scored, key=lambda t: t[0], reverse=True)
            top = [(s, c) for s, c in ranked[:_TEMP_TOP_N] if s > 0.0]
            if len(top) > 1:
                inv_t = 1.0 / max(temperature, 1e-6)
                weights = [s ** inv_t for s, _ in top]
                best_cand = rng.choices([c for _, c in top], weights=weights, k=1)[0]
            elif top:
                best_cand = top[0][1]
        if best_cand is None:
            best_score = -1.0
            for score, c in scored:
                if score > best_score:
                    best_score = score
                    best_cand = c

        if not best_cand:
            break

        next_path = best_cand["path"]
        path_seq.append(next_path)
        visited.add(next_path)
        if mmr_active:
            ev = emb_cache.get(next_path)
            if ev is not None:
                selected_embs.append(ev)
        current = next_path

    return path_seq


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
    """Compact summary used by the assistant's first-open initialisation flow
    and the 'graph health' Settings panel (future). Returns counts per kind
    and a coverage estimate."""
    total_tracks = await db_manager.get_total_tracks()
    return {
        "total_tracks": total_tracks,
        "acoustic_edges": await db_manager.count_neighbors(KIND_ACOUSTIC),
        "artist_edges":   await db_manager.count_neighbors(KIND_ARTIST),
        "album_edges":    await db_manager.count_neighbors(KIND_ALBUM),
    }
