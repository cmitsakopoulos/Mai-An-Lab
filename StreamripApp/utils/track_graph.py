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

The graph is the navigation backbone for the assistant: 'play something
similar' and auto-play open a station (`open_station`, `walk`), 'more by this
artist' reads artist neighbours.

Stations rank ARTISTS by human genre knowledge and let the acoustics choose only
which track plays — see `artist_graph`. The geometry used to carry far more:
first as the ranking itself, gated by tags (`_pool_foreign`, removed), then
ordering the legs of a genre-node journey (`genre_graph`, removed 2026-11-01).
Both leaned on sound to keep genres apart, and sound doesn't: most music shares
harmony and modern production, so the nearest tracks of a house song include
grime, and those of a laiko song include blues-rock.

All builders are async (DB-bound) but the numpy work runs synchronously —
no off-thread call is necessary for libraries up to ~20K tracks; the SVD and the
σ pass are bandwidth-limited and finish in under a second. For very large
libraries the caller should wrap build_acoustic_edges in asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

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
    invalidate_artist_graph_cache()


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
    # Precompute + persist from the artist enrichment cache so stations load it
    # instead of rebuilding it each session. Non-fatal: no enrichment → empty
    # model → tag similarity falls back to compound names and families.
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
    enrichment cache, and drop the stations' artist graph built from the old
    metadata. The one hook every metadata change already runs through (an edit,
    an enrichment pass, an acoustic rebuild). Returns the number of genre pairs
    stored — a graceful 0 when the backend lacks the enrichment accessors (test
    fakes) or there's no enrichment yet."""
    invalidate_artist_graph_cache()
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
        # Per-track tempo and energy, for the stations' segue (0 = unknown).
        "bpm": np.array([float(meta_map[p].get("bpm") or 0.0) for p in paths]),
        "energy": np.array([float(meta_map[p].get("energy") or 0.0) for p in paths]),
        "paths": paths,
        "path_to_idx": path_to_idx,
        "meta_map": meta_map,
    }


# ── Artist graph cache ───────────────────────────────────────────────────────
# The stations' genre knowledge (`artist_graph.ArtistGraph`): artist tag
# profiles and the affinity between them, built from the enrichment and the
# coordinate graph on first use and held for the process. It is never persisted,
# so it cannot go stale behind an edit the way the persisted journey graph did:
# a new coordinate graph changes the cache key, and `build_genre_affinity` —
# which runs after every metadata edit, enrichment pass and acoustic rebuild —
# drops it.
_ARTIST_GRAPH_CACHE: dict = {"key": None, "graph": None}


def invalidate_artist_graph_cache() -> None:
    _ARTIST_GRAPH_CACHE["key"] = None
    _ARTIST_GRAPH_CACHE["graph"] = None


async def _artist_graph_cached(db_manager, coord_graph):
    key = (id(db_manager), id(coord_graph))
    if _ARTIST_GRAPH_CACHE["key"] == key:
        return _ARTIST_GRAPH_CACHE["graph"]
    from utils.artist_graph import ArtistGraph
    paths = coord_graph["paths"]
    display = coord_graph["meta_map"]
    enrich: dict = {}
    if hasattr(db_manager, "get_artist_meta_for_paths"):
        try:
            enrich = await db_manager.get_artist_meta_for_paths(paths)
        except Exception as exc:
            logger.warning("track_graph: stations have no artist metadata: %s", exc)
    npmi: dict = {}
    if hasattr(db_manager, "get_genre_affinity"):
        try:
            npmi = await db_manager.get_genre_affinity() or {}
        except Exception:
            npmi = {}
    meta = []
    for p in paths:
        e = enrich.get(p) or {}
        d = display.get(p) or {}
        meta.append({
            "artist": e.get("artist") or d.get("artist"),
            "credit": d.get("artist"),
            "genres": e.get("genres"),
            "country": e.get("country"),
            "manual": bool(e.get("manual")),
            "names": (d.get("artist"), d.get("album"), d.get("title")),
        })
    # Off the loop: the tag-similarity pass is pure Python over the library's
    # tag vocabulary, long enough on a phone to stall the UI.
    graph = await asyncio.to_thread(
        ArtistGraph, coord_graph["X_unit"], meta, npmi, _credit_key_set,
    )
    _ARTIST_GRAPH_CACHE["key"] = key
    _ARTIST_GRAPH_CACHE["graph"] = graph
    logger.info(
        "track_graph: artist graph built (%d artists, %d tags, %d profiles inferred)",
        len(graph), len(graph.tags), int(graph.inferred.sum()),
    )
    return graph


class Station:
    """A resumable similarity station from one seed track — an
    `artist_graph.Drift` over the library's paths. Every `take(n)` continues
    where the last stopped and never emits a track twice. Auto-play keeps one
    per station; `walk()` is a one-shot `take`. A seed outside the coordinate
    graph (not analysed yet, or no geometry built) gives an empty station."""

    def __init__(self, drift=None, paths: Sequence[str] = (), path_to_idx: Optional[dict] = None):
        self._d = drift
        self._paths = paths
        self._p2i = path_to_idx or {}

    @property
    def flavour(self) -> Optional[str]:
        """Display name of the genre the station is playing now, or None."""
        return self._d.flavour() if self._d is not None else None

    async def take(self, n: int) -> list[str]:
        if self._d is None:
            return []
        return [self._paths[i] for i in self._d.take(n)]

    def _idx(self, paths) -> list[int]:
        return [self._p2i[p] for p in paths if p in self._p2i]

    def exclude(self, paths) -> None:
        """Never emit these (e.g. tracks played from elsewhere meanwhile)."""
        if self._d is not None:
            self._d.exclude(self._idx(paths))

    def accept(self, path: str) -> None:
        """A track listened through: its artist pulls the drift."""
        if self._d is not None and path in self._p2i:
            self._d.accept(self._p2i[path])

    def reject(self, paths) -> None:
        """Negative feedback: never emit `paths`, and steer away from their
        artists' neighbourhood for the rest of this station."""
        if self._d is not None:
            self._d.reject(self._idx(paths))


async def open_station(
    db_manager,
    seed_path: str,
    *,
    avoid: Optional[set[str]] = None,
    focused: bool = False,
    max_per_artist: int = 2,
    max_per_album: int = 1,
    window: int = 0,
) -> Station:
    """Open a station from `seed_path`. `focused` keeps it under the seed's
    genre umbrella (`artist_graph.UMBRELLA`); otherwise it may drift across
    genres. Repeat caps count over the last `window` emitted tracks (0 = the
    whole station). `avoid` is never emitted."""
    from utils import artist_graph as ag

    coord_graph = None
    try:
        coord_graph = await _coord_graph_cached(db_manager)
    except Exception as exc:
        logger.warning("track_graph.open_station: no coordinate graph: %s", exc)
    if coord_graph is None or seed_path not in coord_graph["path_to_idx"]:
        return Station()

    graph = await _artist_graph_cached(db_manager, coord_graph)
    paths = coord_graph["paths"]
    p2i = coord_graph["path_to_idx"]
    display = coord_graph["meta_map"]
    drift = ag.Drift(
        graph, coord_graph["X_unit"], p2i[seed_path],
        exclude=[p2i[p] for p in (avoid or ()) if p in p2i],
        artist_keys=[_credit_key_set((display.get(p) or {}).get("artist")) for p in paths],
        album_keys=[(display.get(p) or {}).get("album") for p in paths],
        caps=ag.RollingCaps(max_per_artist, max_per_album, window),
        focused=focused,
        bpm=coord_graph.get("bpm"), energy=coord_graph.get("energy"),
    )
    return Station(drift, paths, p2i)


async def walk(
    db_manager,
    seed_path: str,
    length: int = 10,
    avoid: Optional[set[str]] = None,
    max_per_artist: int = 2,
    max_per_album: int = 1,
    focused: bool = False,
) -> list[str]:
    """One-shot similarity queue from `seed_path`: a station's first `length`
    tracks (the network view's Walk overlay and Jarvis' "play similar").
    Deterministic. Empty when the seed has no coordinates."""
    station = await open_station(
        db_manager, seed_path,
        avoid=avoid, focused=focused,
        max_per_artist=max_per_artist, max_per_album=max_per_album,
    )
    return await station.take(length)


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
