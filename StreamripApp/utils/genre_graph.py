"""Genre-adjacency graph + journey traversal (PAGA-style).

The seed-ranked walk (`track_graph.walk`) is a *radius*: it ranks the library by
acoustic proximity to the seed and never leaves the seed's genre. This module
builds the scaffolding for a *journey* — a queue that deliberately travels from
the seed's genre into an adjacent one — the explicit mode the walk redesign
parked as Phase 6 idea 1.

Grounding (validated read-only on the 1153-track device image; the productionised
probe is `tools/genre_graph_probe.py`):

  • Genres are NODES. A node is a coarse family (`genre_taxonomy`) optionally
    split by country for regional scenes — "Hip-Hop" vs "Hip-Hop·GR" — because
    the coarse family alone conflates Greek laiko/rap with their Western
    namesakes and produced culturally-incoherent transitions (Vasilis Karras →
    Max Richter). Country split is the cheap "regional nodes" the plan called
    for; the finer laiko/rebetiko tag split is workbench labour.

  • Adjacency is PAGA connectivity, NOT centroid distance: two nodes are
    adjacent iff their tracks actually neighbour each other in the acoustic kNN
    graph (boundary overlap), scored as a lift over chance. Centroid cosine
    happened to agree on the device image, but it cannot represent the
    multi-modal Folk/Cntry node (laiko + Western folk are two blobs whose mean
    is meaningless) and it does not hand you the interface tracks the traversal
    needs. Recorded: the timbre-bridge worry did NOT materialise here — Metal↔
    Hip-Hop lift is 0.17, they avoid each other.

  • Untagged tracks (17% of the image, 81% Greek rap) are placed by kNN
    label-propagation, so they stop being a phantom "(untagged)" node and can
    seed a journey. This is the cheap floor under the metadata workbench: it
    fills the confident cases; only genuinely ambiguous tracks need the human.

Pure numpy + stdlib (+ `genre_taxonomy`), so it compiles for Android like the
rest of the geometry layer. Nothing here touches the DB — the async wrapper and
persistence live in `track_graph`.
"""
from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Optional, Sequence

import numpy as np

from utils.genre_taxonomy import _GENRE_RULES, NON_FAMILIES, genre_bucket

# Countries whose scene is culturally distinct enough to warrant its own node
# per coarse family. GR is the demonstrated pain point (60% of the Greek
# catalogue is untagged and its laiko/rap collide with Western families under a
# shared bucket). Extend as other regional catalogues grow.
REGIONAL_COUNTRIES = frozenset({"GR"})

# Taxonomy priority: rarer / more-specific families first, so a track tagged both
# 'trap' and 'pop' lands in Hip-Hop, not Pop. Mirrors `genre_families`' rationale
# for preferring one primary family per tag over the leaky multi-label view.
_FAMILY_PRIORITY = [label for label, _ in _GENRE_RULES]
_FAMILY_RANK = {f: i for i, f in enumerate(_FAMILY_PRIORITY)}

NODE_SEP = "·"
UNKNOWN_NODE = "Unknown"


# ── Node assignment ──────────────────────────────────────────────────────────

def primary_family(genres) -> Optional[str]:
    """The single coarse family for a track's genre tags, count-weighted then
    tie-broken by taxonomy priority. None when nothing is recognised — the
    signal to label-propagate.

    `genres` is either an iterable of canonical tokens (the alnum form
    `get_artist_meta_for_paths` emits — 'hiphop', 'electropop') or a
    {token: weight} mapping when real tag counts are available. Plain tokens each
    count once, and because the token set carries multiplicity across a family's
    variants, that is enough to keep a Pop track in Pop: 'pop'+'hyperpop'+
    'artpop' outvote a lone 'electropop'→Electronic 3–1. Pure taxonomy priority
    (specific-beats-generic) would instead fold every such track into whatever
    rarer family one stray tag named — measured on the device image, it
    dissolved the Pop node entirely and lost the Electronic→Pop bridge."""
    if isinstance(genres, Mapping):
        items = list(genres.items())
    else:
        items = [(t, 1.0) for t in (genres or ())]
    weights: dict[str, float] = defaultdict(float)
    for tok, w in items:
        fam = genre_bucket(tok)
        if fam not in NON_FAMILIES:
            weights[fam] += float(w)
    if not weights:
        return None
    # heaviest family; ties go to the more specific (lower priority rank)
    return max(
        weights,
        key=lambda f: (weights[f], -_FAMILY_RANK.get(f, len(_FAMILY_PRIORITY))),
    )


def node_label(
    family: Optional[str],
    country: Optional[str],
    regional: frozenset = REGIONAL_COUNTRIES,
) -> Optional[str]:
    """Node id for a track: the family, suffixed with country for regional
    scenes ("Hip-Hop·GR"). A None family stays None so the caller propagates it
    before it is ever turned into a node."""
    if not family:
        return None
    c = (country or "").strip().upper()
    return f"{family}{NODE_SEP}{c}" if c in regional else family


# ── Acoustic kNN (block-chunked; never materialises N×N) ─────────────────────

def knn_graph(X_unit: np.ndarray, k: int = 15, block: int = 1024) -> np.ndarray:
    """Top-k cosine neighbours per row (self excluded). `X_unit` must be
    L2-normalised (as `load_live_coordinate_graph` returns). Block-chunked so the
    N×N similarity is never held whole — memory is O(block·N), matching the
    walk-loader's discipline. Returns an (N, k) int32 array of neighbour indices,
    each row ordered by descending similarity."""
    N = X_unit.shape[0]
    k = min(k, N - 1)
    knn = np.empty((N, k), dtype=np.int32)
    for start in range(0, N, block):
        stop = min(start + block, N)
        sims = X_unit[start:stop] @ X_unit.T          # (b, N)
        # drop self before selecting
        for r in range(stop - start):
            sims[r, start + r] = -np.inf
        part = np.argpartition(-sims, kth=k - 1, axis=1)[:, :k]
        rows = np.arange(stop - start)[:, None]
        order = np.argsort(-sims[rows, part], axis=1)
        knn[start:stop] = part[rows, order]
    return knn


# ── Label propagation (the metadata-workbench floor) ─────────────────────────

def propagate_families(
    families: Sequence[Optional[str]],
    knn: np.ndarray,
    X_unit: np.ndarray,
    min_conf: float = 0.0,
) -> tuple[list[Optional[str]], list[bool], list[float]]:
    """Fill None families by cosine-weighted majority vote of *tagged*
    neighbours. Returns (filled, inferred_mask, confidence).

    Votes are read from the ORIGINAL labels, not the filling ones, so this is a
    single simultaneous pass (an inferred label never becomes evidence for its
    neighbour) — deterministic and order-independent. A track whose neighbours
    are all untagged stays None (left for the caller to mark UNKNOWN); on the
    device image one pass leaves zero homeless. `min_conf` (vote share of the
    winning family) gates auto-acceptance so ambiguous tracks can be routed to
    the workbench instead."""
    filled = list(families)
    inferred = [False] * len(families)
    conf = [1.0 if f is not None else 0.0 for f in families]
    for i, f in enumerate(families):
        if f is not None:
            continue
        nbr = knn[i]
        w = X_unit[nbr] @ X_unit[i]                    # cosine to each neighbour
        vote: dict[str, float] = defaultdict(float)
        for j, wj in zip(nbr, w):
            fj = families[int(j)]
            if fj is not None:
                vote[fj] += max(float(wj), 0.0)
        if not vote:
            continue
        total = sum(vote.values())
        best, score = max(vote.items(), key=lambda kv: kv[1])
        c = score / total if total > 0 else 0.0
        if c >= min_conf:
            filled[i] = best
            inferred[i] = True
            conf[i] = c
    return filled, inferred, conf


# ── PAGA connectivity ────────────────────────────────────────────────────────

def paga_connectivity(nodes: Sequence[str], knn: np.ndarray):
    """Symmetric lift-over-chance between nodes from kNN edge crossing.

    lift[a, b] > 1  ⇒ a and b's tracks neighbour each other more than a random
    graph of the same node sizes would predict — they touch / are adjacent.
    < 1 ⇒ they avoid. Returns (node_order, idx, lift, sizes)."""
    N, k = knn.shape
    sizes = Counter(nodes)
    order = [n for n, _ in sizes.most_common()]
    idx = {n: i for i, n in enumerate(order)}
    M = len(order)
    lab = np.array([idx[n] for n in nodes])
    src = np.repeat(lab, k)
    dst = lab[knn.reshape(-1)]
    obs = np.zeros((M, M))
    np.add.at(obs, (src, dst), 1.0)
    sz = np.array([sizes[n] for n in order], dtype=float)
    exp = (sz[:, None] * k) * (sz[None, :] / (N - 1))
    obs_s, exp_s = obs + obs.T, exp + exp.T
    with np.errstate(divide="ignore", invalid="ignore"):
        lift = np.where(exp_s > 0, obs_s / exp_s, 0.0)
    return order, idx, lift, sizes


def adjacency(order, idx, lift, sizes, min_size: int = 10, min_lift: float = 1.0):
    """Per node (with ≥ min_size tracks), its other ≥ min_size nodes ranked by
    connectivity, keeping only genuine adjacencies (lift ≥ min_lift). Tail nodes
    get no edges — their tracks fall back to a pure radius, which is the honest
    behaviour for a genre with no real neighbour (e.g. an insular Hip-Hop
    majority whose best exit is below chance)."""
    big = [n for n in order if sizes[n] >= min_size]
    adj: dict[str, list[tuple[str, float]]] = {}
    for a in big:
        ia = idx[a]
        ranked = sorted(
            ((float(lift[ia, idx[b]]), b) for b in big if b != a), reverse=True
        )
        adj[a] = [(b, l) for l, b in ranked if l >= min_lift]
    return adj


# ── Journey traversal ────────────────────────────────────────────────────────

def _split(total: int, parts: int) -> list[int]:
    base, rem = divmod(total, parts)
    return [base + (1 if i < rem else 0) for i in range(parts)]


def _choose_next(current, adj, visited, rng, jump_temp):
    """Pick the node to jump to. Deterministic argmax when rng is None; otherwise
    softmax-sample over log-lift / temperature so repeat presses vary without
    abandoning the ranking. Never revisits a node already in the journey."""
    cand = [(b, l) for b, l in adj.get(current, []) if b not in visited]
    if not cand:
        return None
    if rng is None or jump_temp <= 0:
        return cand[0][0]
    lifts = np.array([l for _, l in cand], dtype=float)
    w = np.exp(np.log(np.maximum(lifts, 1e-6)) / jump_temp)
    w /= w.sum()
    r = rng.random()
    cum = 0.0
    for (b, _), wi in zip(cand, w):
        cum += float(wi)
        if r <= cum:
            return b
    return cand[-1][0]


def _fill_leg(ref, X_unit, nodes, node, used, want, cap_ok):
    """Emit up to `want` unused tracks nearest to `ref`, restricted to `node`
    (None = any node, the radius fallback). A track the cap rejects is skipped
    and permanently excluded — over-cap means never-reconsider, so the leg can't
    stall on a dominant artist. `cap_ok` commits the cap counters on accept."""
    if want <= 0:
        return []
    order = np.argsort(-(X_unit @ X_unit[ref]))
    out = []
    for j in order:
        j = int(j)
        if j in used or (node is not None and nodes[j] != node):
            continue
        if not cap_ok(j):
            used.add(j)
            continue
        out.append(j)
        used.add(j)
        if len(out) >= want:
            break
    return out


def _pick_bridge(ref, X_unit, nodes, node, used, rng, pool, cap_ok):
    """The interface track: among the `pool` members of `node` nearest to `ref`,
    the one that passes the cap — nearest-first when deterministic, shuffled when
    an rng is given (stochastic entry, the anti-repetition knob). Enters B near
    where A was left."""
    cands = []
    for j in np.argsort(-(X_unit @ X_unit[ref])):
        j = int(j)
        if j in used or nodes[j] != node:
            continue
        cands.append(j)
        if len(cands) >= max(pool, 1):
            break
    if rng is not None and len(cands) > 1:
        rng.shuffle(cands)
    for j in cands:
        if cap_ok(j):
            used.add(j)
            return j
        used.add(j)
    return None


def journey(
    seed_idx: int,
    X_unit: np.ndarray,
    nodes: Sequence[str],
    adj: dict,
    *,
    length: int = 10,
    hops: int = 1,
    rng: Optional[random.Random] = None,
    jump_temp: float = 0.5,
    pool: int = 3,
    exclude: Optional[set] = None,
    artist_keys: Optional[Sequence] = None,
    album_keys: Optional[Sequence] = None,
    max_per_artist: int = 0,
    max_per_album: int = 0,
) -> list[int]:
    """An ordered list of track indices that starts at the seed, fills a leg
    within the seed's node (radius), then hops into an adjacent node *through its
    interface* (the member nearest the last track — "leave A near B, enter B near
    A") and fills a leg there.

    `length` (including the seed at index 0) is split evenly across `hops + 1`
    legs. `rng` makes the jump target and interface entry stochastic — the fix
    for deterministic, repetitive queues; pass None for a reproducible walk.

    Repeat caps are enforced *during* selection (not as a post-filter, which
    would truncate a leg): give `artist_keys[i]` (a frozenset of credit keys)
    and/or `album_keys[i]` aligned to the rows, plus the max counts. The seed is
    not counted, matching the radius walk's "cap the emitted queue, seed
    excluded" contract.

    Degrades to a pure radius whenever a node has no admissible adjacency or runs
    out of members, so it never returns short by construction."""
    used = set(exclude or ())
    used.add(seed_idx)
    artist_hits: dict = {}
    album_hits: dict = {}

    def cap_ok(j: int) -> bool:
        keys = artist_keys[j] if artist_keys is not None else ()
        if max_per_artist > 0 and keys and any(
            artist_hits.get(k, 0) >= max_per_artist for k in keys
        ):
            return False
        alb = album_keys[j] if album_keys is not None else None
        if max_per_album > 0 and alb and album_hits.get(alb, 0) >= max_per_album:
            return False
        for k in keys:
            artist_hits[k] = artist_hits.get(k, 0) + 1
        if alb:
            album_hits[alb] = album_hits.get(alb, 0) + 1
        return True

    result = [seed_idx]
    ref = seed_idx
    current = nodes[seed_idx]
    visited = {current}
    legs = _split(length, hops + 1)

    result += _fill_leg(ref, X_unit, nodes, current, used, legs[0] - 1, cap_ok)
    if len(result) > 1:
        ref = result[-1]

    for h in range(hops):
        if len(result) >= length:
            break
        want = legs[h + 1]
        nxt = _choose_next(current, adj, visited, rng, jump_temp)
        if nxt is None:
            # No admissible adjacency: extend the radius WITHIN the current node
            # (more of the seed's own genre) rather than filling from ANY node.
            # A pure-cosine fill across all nodes is exactly the leak that lets an
            # acoustically-close but unrelated genre (the Hip-Hop→Electronic
            # timbre bridge) ride into the queue. Only pad from outside the node
            # if the node itself is exhausted, so the length guarantee still holds.
            more = _fill_leg(ref, X_unit, nodes, current, used, want, cap_ok)
            if len(more) < want:
                more += _fill_leg(
                    ref, X_unit, nodes, None, used, want - len(more), cap_ok
                )
            result += more
            if more:
                ref = result[-1]
            continue
        bridge = _pick_bridge(ref, X_unit, nodes, nxt, used, rng, pool, cap_ok)
        visited.add(nxt)
        current = nxt
        if bridge is None:
            continue
        result.append(bridge)
        ref = bridge
        rest = _fill_leg(ref, X_unit, nodes, nxt, used, want - 1, cap_ok)
        result += rest
        if rest:
            ref = result[-1]

    return result[:length]


# ── One-shot build (the cacheable payload) ───────────────────────────────────

def build_genre_graph(
    X_unit: np.ndarray,
    meta: Sequence[dict],
    *,
    k: int = 15,
    regional: frozenset = REGIONAL_COUNTRIES,
    min_size: int = 10,
    min_lift: float = 1.0,
    min_conf: float = 0.0,
):
    """Assemble the journey scaffolding from L2-normalised coords + per-track
    metadata. `meta[i]` is `{'genres': <tokens>, 'country': <str>}` aligned to
    the rows of `X_unit`. Returns a dict:

        nodes       list[str]  per track, post-propagation (never None)
        inferred    list[bool] which node labels came from propagation
        adj         dict       node -> [(node, lift), ...] adjacency
        order/idx/lift/sizes   the raw PAGA connectivity, for diagnostics

    This is the once-per-rebuild payload; the per-press journey only needs
    `nodes` + `adj` + the live `X_unit`."""
    knn = knn_graph(X_unit, k=k)
    families = [primary_family(m.get("genres")) for m in meta]
    families, inferred, _conf = propagate_families(families, knn, X_unit, min_conf)
    nodes = [
        node_label(fam, meta[i].get("country"), regional) or UNKNOWN_NODE
        for i, fam in enumerate(families)
    ]
    order, idx, lift, sizes = paga_connectivity(nodes, knn)
    adj = adjacency(order, idx, lift, sizes, min_size=min_size, min_lift=min_lift)
    return {
        "nodes": nodes,
        "inferred": inferred,
        "adj": adj,
        "order": order,
        "idx": idx,
        "lift": lift,
        "sizes": sizes,
    }
