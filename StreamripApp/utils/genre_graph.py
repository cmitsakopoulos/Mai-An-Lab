"""Genre-adjacency graph + journey traversal (PAGA-style).

The seed-ranked walk (`track_graph.walk`) is a *radius*: it ranks the library by
acoustic proximity to the seed and never leaves the seed's genre. This module
builds the scaffolding for a *journey* — a queue that deliberately travels from
the seed's genre into an adjacent one — the explicit mode the walk redesign
parked as Phase 6 idea 1.

Grounding (validated read-only on the 1153-track device image; the productionised
probe is `tools/genre_graph_probe.py`):

  • Genres are NODES. A node is a coarse family (`genre_taxonomy`) split by
    CULTURE for regional scenes — "Hip-Hop" vs "Hip-Hop·GR" — because the
    coarse family alone conflates Greek laiko/rap with their Western namesakes
    and produced culturally-incoherent transitions (Vasilis Karras → Max
    Richter). Culture comes from country, scene tags or the script of the
    artist / album / track names (`track_culture`), per artist.

  • Adjacency is BRIDGE ARTISTS, not acoustics: two nodes are adjacent when at
    least `min_bridges` of the library's artists carry both genres in their own
    tags (Alice In Chains: Rock + Metal; Depeche Mode: Alt + Electronic + Pop),
    scored as a lift over chance. Nodes of different cultures are never
    adjacent. The acoustic kNN lift this replaced (2026-09-28, 474-track device
    image) was artist-dominated — Metal, Alt and Rock·GR had no exit at all — and
    on nodes of 10–22 tracks a couple of kNN edges produced the Classical → Pop
    → laiko chain. Acoustics still order the tracks inside a leg and pick which
    bridge track carries a hop.

  • Untagged tracks (17% of the image, 81% Greek rap) are placed by kNN
    label-propagation, so they stop being a phantom "(untagged)" node and can
    seed a journey. This is the cheap floor under the metadata workbench: it
    fills the confident cases; only genuinely ambiguous tracks need the human.

Pure numpy + stdlib (+ `genre_taxonomy`), so it compiles for Android like the
rest of the geometry layer. Nothing here touches the DB — the async wrapper and
persistence live in `track_graph`.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from typing import Optional, Sequence

import numpy as np

from utils.genre_taxonomy import (
    _GENRE_RULES, CULTURE_WORDS, NON_FAMILIES, genre_bucket, script_culture, tag_culture,
)

# Cultures distinct enough to warrant their own node per coarse family, and a
# hard boundary for the journey. GR is the demonstrated pain point (its
# laiko/rap collide with Western families under a shared bucket). Extend as
# other regional catalogues grow (with scripts / scene tags in genre_taxonomy).
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
        if _is_culture_word(tok):
            continue
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


def _is_culture_word(tok) -> bool:
    return "".join(ch for ch in str(tok).lower() if ch.isalnum()) in CULTURE_WORDS


def artist_families(genres) -> set[str]:
    """Every coarse family an artist's own tags name — the bridge evidence. Two
    thirds of the device image's tagged artists span ≥ 2 families."""
    out = set()
    for tok in genres or ():
        if _is_culture_word(tok):
            continue
        fam = genre_bucket(tok)
        if fam not in NON_FAMILIES:
            out.add(fam)
    return out


def track_culture(
    country: Optional[str],
    genres=(),
    names=(),
    regional: frozenset = REGIONAL_COUNTRIES,
) -> Optional[str]:
    """The regional culture a track belongs to, or None (the borderless
    majority). Any one signal is enough: the enrichment country, a scene tag
    ('laiko'), or names written in the culture's script. Country comes first but
    does not veto the others — it is missing or wrong (a short name matched to
    the wrong MusicBrainz entity) exactly for the local acts this protects."""
    c = (country or "").strip().upper()
    if c in regional:
        return c
    for tok in genres or ():
        cu = tag_culture(tok)
        if cu in regional:
            return cu
    cu = script_culture(*names)
    return cu if cu in regional else None


def node_label(family: Optional[str], culture: Optional[str] = None) -> Optional[str]:
    """Node id: the family, suffixed with a regional culture ("Hip-Hop·GR"). A
    None family stays None so the caller propagates it before it is ever turned
    into a node."""
    if not family:
        return None
    c = (culture or "").strip().upper()
    return f"{family}{NODE_SEP}{c}" if c else family


# Display names for culture suffixes.
CULTURE_NAMES = {"GR": "Greek"}


def node_display(node: Optional[str]) -> str:
    """Human label for a node id: "Folk/Cntry·GR" → "Greek Folk/Cntry"."""
    if not node:
        return ""
    fam, _, culture = node.partition(NODE_SEP)
    return f"{CULTURE_NAMES.get(culture, culture)} {fam}" if culture else fam


def node_culture(node: Optional[str]) -> Optional[str]:
    """The culture suffix of a node id (None for a borderless node)."""
    if not node or NODE_SEP not in node:
        return None
    return node.split(NODE_SEP, 1)[1] or None


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


# ── Bridge-artist adjacency ──────────────────────────────────────────────────

# Edges need this many bridge artists; one artist's tags are too noisy on their
# own (Nickelback's span five families). A node with fewer than
# MIN_NODE_ARTISTS artists can't meet that bar however related it is — a genre
# the listener owns one artist of — so there one bridge is enough.
MIN_BRIDGES = 2
MIN_NODE_ARTISTS = 3
# Below half of chance, two bridge artists are what two big families share by
# accident — crossover acts and stray tags (on the device image: Hip-Hop's only
# ties were Limp Bizkit / Linkin Park at 0.4, Rock–Electronic was Nickelback's
# stray 'electronic' at 0.24), not a neighbourhood. Real but hub-diluted ties
# clear it (Alt–Electronic via Depeche Mode / New Order, 0.62).
MIN_LIFT = 0.5


def bridge_adjacency(
    nodes: Sequence[str],
    track_artists: Sequence[str],
    memberships: Mapping[str, set],
    *,
    min_size: int = 10,
    min_bridges: int = MIN_BRIDGES,
    min_node_artists: int = MIN_NODE_ARTISTS,
    min_lift: float = MIN_LIFT,
):
    """Adjacency between nodes of ≥ `min_size` tracks from the artists they
    share. `memberships[artist]` is every node the artist belongs to — where its
    tracks sit plus every family its tags name, in its own culture.

    Returns (adj, via): adj[node] = [(node, lift), ...] strongest first, and
    via[(a, b)] = the bridge artists (sorted). Lift is shared artists over what
    chance predicts from both nodes' artist counts, so a hub like Rock (half the
    library's artists carry a rock tag) doesn't outrank a specific tie. Nodes of
    different cultures are never adjacent.

    An edge that only exists through the small-node allowance is ONE-WAY, out of
    the small node: it is there so a genre the listener owns one artist of is
    not a dead end, and a single artist's lift is huge by construction — two-way,
    every Electronic station would hop into Max Richter."""
    sizes = Counter(nodes)
    big = sorted(n for n, c in sizes.items() if c >= min_size and n != UNKNOWN_NODE)
    home: dict[str, set] = defaultdict(set)
    for n, a in zip(nodes, track_artists):
        home[n].add(a)
    members: dict[str, set] = defaultdict(set)
    population: Counter = Counter()
    for a, ns in memberships.items():
        for n in ns:
            members[n].add(a)
        for c in {node_culture(n) for n in ns}:
            population[c] += 1
    adj: dict[str, list[tuple[str, float]]] = {n: [] for n in big}
    via: dict[tuple[str, str], list[str]] = {}
    for i, a in enumerate(big):
        for b in big[i + 1:]:
            culture = node_culture(a)
            if culture != node_culture(b):
                continue
            shared = members[a] & members[b]
            if not shared:
                continue
            lift = len(shared) * population[culture] / (len(members[a]) * len(members[b]))
            if lift < min_lift:
                continue
            if len(shared) >= min_bridges:
                ways = ((a, b), (b, a))
            else:
                ways = tuple((x, y) for x, y in ((a, b), (b, a))
                             if len(home[x]) < min_node_artists)
            for x, y in ways:
                adj[x].append((y, lift))
                via[(x, y)] = sorted(shared)
    for n in adj:
        adj[n].sort(key=lambda e: (-e[1], e[0]))
    return adj, via


# ── Journey traversal ────────────────────────────────────────────────────────

# Rank-sampling strength for a random station: pick the k-th best admissible
# candidate with P(k) ∝ RANK_DECAY**k. Measured on the 474-track device image
# (2026-09-28): 0.6 makes two stations from one seed share ~40% of tracks for a
# 4% drop in similarity to the leg reference; 0.8 costs 14% for ~8 points more
# variety, and uniform sampling ruins the queue. Sampling by RANK, not score, is
# deliberate: near the top the cosines are nearly equal, so a score temperature
# collapsed to "uniform over the top ~6" at every setting (the old walk slider).
RANK_DECAY = 0.6
# How many admissible candidates a random pick looks at. P(k ≥ 16) at 0.6 is
# 0.03%, so the truncation is invisible while bounding the scan.
_LOOKAHEAD = 16


def rank_pick(n: int, rng, decay: float = RANK_DECAY) -> int:
    """Index into a best-first list of `n` candidates: 0 without an `rng`, else
    k with P(k) ∝ decay**k (a geometric draw, truncated to the list)."""
    if rng is None or n <= 1 or not 0.0 < decay < 1.0:
        return 0
    r = rng.random() * (1.0 - decay ** n)
    k = int(math.floor(math.log(1.0 - r) / math.log(decay)))
    return min(max(k, 0), n - 1)


class RollingCaps:
    """Repeat limits over the last `window` emitted tracks (0 = the whole run):
    at most `max_per_artist` per act and `max_per_album` per release. Artists
    are credit-key sets, so a collab counts against every act it names. Acts in
    `banned` never pass. A limit of 0 disables it.

    Same-artist runs across albums are wanted (a refreshing slice of a
    discography); what the album limit prevents is a walk stuck inside one
    release. The window keeps a long station from locking an act out for good."""

    def __init__(self, max_per_artist: int = 0, max_per_album: int = 0, window: int = 0):
        self.max_per_artist = max_per_artist
        self.max_per_album = max_per_album
        self._recent: Optional[deque] = deque() if window > 0 else None
        self._window = window
        self._artists: Counter = Counter()
        self._albums: Counter = Counter()
        self.banned: set = set()

    def ok(self, keys, album) -> bool:
        keys = keys or ()
        if self.banned and any(k in self.banned for k in keys):
            return False
        if self.max_per_artist > 0 and any(
            self._artists[k] >= self.max_per_artist for k in keys
        ):
            return False
        if self.max_per_album > 0 and album and self._albums[album] >= self.max_per_album:
            return False
        return True

    def add(self, keys, album) -> None:
        keys = tuple(keys or ())
        if self._recent is not None:
            if len(self._recent) >= self._window:
                old_keys, old_album = self._recent.popleft()
                for k in old_keys:
                    self._artists[k] -= 1
                if old_album:
                    self._albums[old_album] -= 1
            self._recent.append((keys, album))
        for k in keys:
            self._artists[k] += 1
        if album:
            self._albums[album] += 1


def _choose(options, rng, jump_temp):
    """Pick one of `options` [(node, score), ...] (best first). Deterministic
    argmax when rng is None; otherwise softmax-sample over log-score /
    temperature, so stations vary without abandoning the ranking."""
    if not options:
        return None
    if rng is None or jump_temp <= 0:
        return options[0][0]
    scores = np.array([sc for _, sc in options], dtype=float)
    w = np.exp(np.log(np.maximum(scores, 1e-6)) / jump_temp)
    w /= w.sum()
    r = rng.random()
    cum = 0.0
    for (b, _), wi in zip(options, w):
        cum += float(wi)
        if r <= cum:
            return b
    return options[-1][0]


# How many tracks after a hop (the bridge included) are blamed on the hop when
# the listener rejects them.
HOP_WINDOW = 3
# Floor on the entry fit, so an exit whose best entry is acoustically far (or
# repelled below zero) stays possible, just unlikely.
_MIN_FIT = 0.05


class Journey:
    """A resumable traversal of the genre-adjacency graph: legs of `leg_len`
    tracks inside one node, each ranked by proximity to the leg's reference
    track, then a hop into an adjacent node through a BRIDGE track — by an
    artist whose tags span both genres (`via`), nearest where the last leg
    ended; failing that, the target node's own track nearest there.

    Resumable is the point: an auto-play station calls `take()` for each refill
    and continues where it left off — same node, same leg, nothing emitted twice
    — instead of re-walking from the seed (which re-queued the tracks a user had
    just removed). `end_leg()` is the negative-feedback hook: the next take hops
    on and steers AWAY from the rejected tracks (`repel`).

    Deterministic without an `rng` (nearest first, strongest adjacency, nearest
    entry). With one: rank sampling inside legs (`rank_pick`), a lift-weighted
    jump, and a shuffled entry among the `pool` nearest interface tracks.

    Which hop: every admissible exit is scored by its lift × the listener's
    weight × how near its best entry track sits to where the leg ended (the
    genre graph says WHERE a station may go, acoustics say which of those fits
    now — a Depeche Mode leg in Alt leaves for Rock through A Flock Of Seagulls,
    not for Metal through Alice In Chains).

    A node with no admissible exit keeps going in a fresh leg of its own; one
    that is exhausted pads from its adjacent nodes first, then from anywhere in
    its culture, so a take only returns short when that is used up. Nodes
    visited in the last `revisit_after` legs are not jumped back to.

    Learned hop penalties: `hop_weights[(a, b)]` scales that hop (0 blocks it),
    and `hop_of[track]` names the hop a track came in on for the first
    `hop_window` tracks after it — what a rejection of that track is blamed
    on."""

    def __init__(
        self,
        seed_idx: int,
        X_unit: np.ndarray,
        nodes: Sequence[str],
        adj: dict,
        *,
        exclude=(),
        artist_keys: Optional[Sequence] = None,
        album_keys: Optional[Sequence] = None,
        caps: Optional[RollingCaps] = None,
        rng: Optional[random.Random] = None,
        decay: float = RANK_DECAY,
        jump_temp: float = 0.5,
        pool: int = 3,
        leg_len: int = 8,
        repel: float = 0.5,
        revisit_after: int = 3,
        via: Optional[Mapping] = None,
        hop_weights: Optional[Mapping] = None,
        hop_window: int = HOP_WINDOW,
    ):
        self.X = X_unit
        self.nodes = nodes
        self.adj = adj
        self.artist_keys = artist_keys
        self.album_keys = album_keys
        self.caps = caps or RollingCaps()
        self.rng = rng
        self.decay = decay
        self.jump_temp = jump_temp
        self.pool = pool
        self.leg_len = max(1, leg_len)
        self.repel = repel
        self.used: set = set(exclude)
        self.used.add(seed_idx)
        self.node = nodes[seed_idx]
        self.ref = seed_idx          # what the current leg is ranked against
        self._last = seed_idx        # the most recently emitted track
        self._leg_n = 0
        self._recent_nodes: deque = deque([self.node], maxlen=max(1, revisit_after))
        self._rejected: deque = deque(maxlen=8)
        self.via = via or {}                     # (a, b) -> bridge artists' keys
        self.hop_weights: dict = dict(hop_weights or {})
        self.hop_window = hop_window
        self.hop_of: dict[int, tuple[str, str]] = {}
        self._hop: Optional[tuple[str, str]] = None
        self._hop_left = 0

    # ── public ──────────────────────────────────────────────────────────────
    def take(self, n: int) -> list[int]:
        out: list[int] = []
        while len(out) < n:
            if self._leg_n >= self.leg_len:
                bridge = self._next_leg()
                if bridge is not None:
                    out.append(bridge)
                    continue
            j = self._pick(self._in_node)
            if j is None:
                # The node is used up (for now — rolling caps may free it later).
                bridge = self._next_leg()
                if bridge is not None:
                    out.append(bridge)
                    continue
                near = {b for b, _ in self.adj.get(self.node, [])
                        if self.hop_weights.get((self.node, b), 1.0) > 0}
                j = self._pick(lambda k: self.nodes[k] in near)
                if j is None:
                    culture = node_culture(self.node)
                    j = self._pick(lambda k: node_culture(self.nodes[k]) == culture)
                if j is None:
                    break
            self._emit(j)
            out.append(j)
        return out

    def end_leg(self, reject: Sequence[int] = ()) -> None:
        """Negative feedback: remember `reject` (every later ranking is pushed
        away from them) and make the next take start a new leg."""
        for j in reject:
            self._rejected.append(int(j))
            self.used.add(int(j))
        self._leg_n = self.leg_len

    def exclude(self, idxs) -> None:
        self.used.update(int(j) for j in idxs)

    def weight_hop(self, src: str, dst: str, weight: float) -> None:
        """Scale the src → dst hop from now on (0 = never take it)."""
        self.hop_weights[(src, dst)] = weight

    def accept(self, idx: int) -> None:
        """A track listened through: if it belongs to the current leg's node it
        becomes the leg's reference, so the station drifts only along songs the
        listener accepted."""
        if 0 <= idx < len(self.nodes) and self.nodes[idx] == self.node:
            self.ref = idx

    # ── internals ───────────────────────────────────────────────────────────
    def _in_node(self, j: int) -> bool:
        return self.nodes[j] == self.node

    def _keys(self, j: int):
        return self.artist_keys[j] if self.artist_keys is not None else frozenset()

    def _album(self, j: int):
        return self.album_keys[j] if self.album_keys is not None else None

    def _scores(self, ref: int) -> np.ndarray:
        s = self.X @ self.X[ref]
        if self._rejected and self.repel > 0:
            s = s - self.repel * (self.X @ self.X[list(self._rejected)].T).max(axis=1)
        return s

    def _candidates(self, ref: int, admit, limit: int) -> list[int]:
        out = []
        for j in np.argsort(-self._scores(ref)):
            j = int(j)
            if j in self.used or not admit(j):
                continue
            if not self.caps.ok(self._keys(j), self._album(j)):
                continue
            out.append(j)
            if len(out) >= limit:
                break
        return out

    def _pick(self, admit) -> Optional[int]:
        limit = 1 if self.rng is None else _LOOKAHEAD
        cands = self._candidates(self.ref, admit, limit)
        if not cands:
            return None
        return cands[rank_pick(len(cands), self.rng, self.decay)]

    def _emit(self, j: int) -> None:
        self.used.add(j)
        self.caps.add(self._keys(j), self._album(j))
        self._last = j
        self._leg_n += 1
        if self._hop is not None and self._hop_left > 0:
            self.hop_of[j] = self._hop
            self._hop_left -= 1

    def _entries(self, b: str) -> list[int]:
        """The nearest tracks (to where the leg ended) that can carry a hop into
        `b`: by one of the hop's bridge artists, else `b`'s own."""
        pool = max(self.pool, 1)
        keys = self.via.get((self.node, b))
        cands = []
        if keys:
            cands = self._candidates(self._last, lambda j: not keys.isdisjoint(self._keys(j)), pool)
        return cands or self._candidates(self._last, lambda j: self.nodes[j] == b, pool)

    def _next_leg(self) -> Optional[int]:
        """Start a new leg. Hops to an adjacent node when one is admissible and
        has an entry track (returns the emitted bridge); otherwise stays in this
        node, re-anchored on the last track (returns None)."""
        self._leg_n = 0
        self._hop = None
        scores = self._scores(self._last)
        options, entries = [], {}
        for b, lift in self.adj.get(self.node, []):
            w = self.hop_weights.get((self.node, b), 1.0)
            if b in self._recent_nodes or w <= 0:
                continue
            cands = self._entries(b)
            if cands:
                entries[b] = cands
                options.append((b, lift * w * max(float(scores[cands[0]]), _MIN_FIT)))
        options.sort(key=lambda e: -e[1])
        nxt = _choose(options, self.rng, self.jump_temp)
        if nxt is not None:
            cands = entries[nxt]
            if self.rng is not None:
                self.rng.shuffle(cands)
            bridge = cands[0]
            self._hop, self._hop_left = (self.node, nxt), self.hop_window
            self.node = nxt
            self._recent_nodes.append(nxt)
            self.ref = bridge
            self._emit(bridge)
            return bridge
        self.ref = self._last
        return None


# ── One-shot build (the cacheable payload) ───────────────────────────────────

def build_genre_graph(
    X_unit: np.ndarray,
    meta: Sequence[dict],
    *,
    k: int = 15,
    regional: frozenset = REGIONAL_COUNTRIES,
    min_size: int = 10,
    min_bridges: int = MIN_BRIDGES,
    min_node_artists: int = MIN_NODE_ARTISTS,
    min_conf: float = 0.0,
):
    """Assemble the journey scaffolding from L2-normalised coords + per-track
    metadata. `meta[i]` is `{'genres': <tokens>, 'country': <str>, 'artist':
    <str>, 'names': (artist, album, title)}` aligned to the rows of `X_unit`;
    genres and country are artist-level (enrichment). Returns a dict:

        nodes       list[str]  per track, post-propagation (never None)
        inferred    list[bool] which node labels came from propagation
        adj         dict       node -> [(node, lift), ...] adjacency
        via         dict       (node, node) -> bridge artist names
        sizes       Counter    tracks per node

    Culture is decided per ARTIST (over all its tracks' names), so an act never
    straddles two cultures. The acoustic kNN serves only label propagation of
    untagged tracks."""
    knn = knn_graph(X_unit, k=k)
    families = [primary_family(m.get("genres")) for m in meta]
    families, inferred, _conf = propagate_families(families, knn, X_unit, min_conf)

    artists = [m.get("artist") or f"#{i}" for i, m in enumerate(meta)]
    tracks_of: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(artists):
        tracks_of[a].append(i)
    # Genres and country are artist-level; take the first track that has them.
    def first(idxs, key):
        return next((meta[i].get(key) for i in idxs if meta[i].get(key)), None)

    culture: dict[str, Optional[str]] = {}
    for a, idxs in tracks_of.items():
        names = [n for i in idxs for n in (meta[i].get("names") or ())]
        culture[a] = track_culture(first(idxs, "country"), first(idxs, "genres"), names, regional)

    nodes = [
        node_label(fam, culture[artists[i]]) or UNKNOWN_NODE
        for i, fam in enumerate(families)
    ]
    memberships: dict[str, set] = defaultdict(set)
    for i, a in enumerate(artists):
        memberships[a].add(nodes[i])
    for a, idxs in tracks_of.items():
        for fam in artist_families(first(idxs, "genres")):
            memberships[a].add(node_label(fam, culture[a]))
    adj, via = bridge_adjacency(
        nodes, artists, memberships, min_size=min_size,
        min_bridges=min_bridges, min_node_artists=min_node_artists,
    )
    return {
        "nodes": nodes,
        "inferred": inferred,
        "adj": adj,
        "via": via,
        "sizes": Counter(nodes),
    }
