"""Artist-affinity stations: what auto-play and "play similar" queue next.

A queue is a DRIFT through the library's artists, ranked by what human genre
knowledge says belongs together. Sound only picks which of an artist's tracks
plays (the smoothest segue: `Drift._segue`) and breaks ties between artists that
the tags can't tell apart.

Why artists and tags, not genre nodes or acoustics (measured on the 1142-track
device image, 2026-11-01):

  • Sound does not separate genres, not even averaged over a genre. The centroid
    of the 'house' tracks sits at cosine 0.91 to 'grime' and 0.86 to 'trap',
    'laiko' at 0.77 to 'bluesrock'. Most music shares harmony and modern
    production, so acoustics link things no listener would put together.
  • A hard genre partition fails the other way. One label per track (an argmax
    over noisy tags) put 54% of the artist pairs that share a genre family into
    different nodes, and node-to-node adjacency from bridge artists left Hip-Hop
    (44% of the library) with no exit at all.

So genre is SOFT here. Each artist is a profile over its genre tags, and two
artists are as close as their profiles under a tag-to-tag similarity built only
from human sources (`tag_similarity`): tags that co-tag the same artists (the
NPMI model), compound names ('deephouse' is a kind of 'house'), and the coarse
taxonomy family. House, electro and dance are not merged; they are near each
other, so a queue moves among them without crossing anything. Collaborations
(Dave & AJ Tracey) are the strongest evidence of all and link artists directly.

Transitions come from the drift (`Drift`): each pick is ranked against the seed
artist AND the last few artists queued, and the seed's share decays with every
track down to a floor. A house station leaves house only through artists close
to what it has just been playing, one overlapping step at a time, and never
strays far from where it started.

Pure numpy + stdlib, so it compiles for Android with the rest of the geometry
layer. Nothing here touches the DB; `track_graph` loads and caches.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from typing import Callable, Optional, Sequence

import numpy as np

from utils.genre_taxonomy import (
    CULTURE_WORDS, NON_FAMILIES, genre_bucket, genre_display_label, is_regional_tag,
    script_culture, tag_culture,
)

# ── Tag similarity ───────────────────────────────────────────────────────────

# Two tags in the same coarse family ('trap' and 'drill') are related even when
# no artist in this library carries both. Not across the regional line: the
# Folk/Cntry family holds laiko and blues alike, and that is a filing choice, not
# a kinship (it ran a laiko station into Fleetwood Mac).
FAMILY_SIM = 0.3
# A compound genre is a kind of its head: 'deephouse' → 'house', 'poprap' →
# 'rap', 'folkmetal' → 'metal'. English genre names put the head LAST, so only a
# suffix counts ('alternativehiphop' is hip-hop, not 'alternative'), and both
# parts must be ≥ 3 letters ('garage' does not end in a genre called 'rage').
ROOT_SIM = 0.6
_MIN_ROOT = 3


def _is_compound_of(tag: str, head: str) -> bool:
    return (len(head) >= _MIN_ROOT and len(tag) - len(head) >= _MIN_ROOT
            and tag.endswith(head))


def _family(tag: str) -> Optional[str]:
    fam = genre_bucket(tag)
    return None if fam in NON_FAMILIES else fam


def tag_similarity(tags: Sequence[str], npmi: Optional[dict] = None) -> np.ndarray:
    """(T, T) similarity in [0, 1] between canonical genre tokens: the strongest
    of co-tagging (the support-shrunk NPMI model, {"a|b": v}), compound head and
    shared family. Self = 1. Deliberately no acoustic term (see module doc)."""
    T = len(tags)
    S = np.zeros((T, T), dtype=np.float32)
    fams = [_family(t) for t in tags]
    regional = [is_regional_tag(t) for t in tags]
    for i, a in enumerate(tags):
        for j in range(i + 1, T):
            b = tags[j]
            s = 0.0
            if fams[i] is not None and fams[i] == fams[j] and regional[i] == regional[j]:
                s = FAMILY_SIM
            if _is_compound_of(a, b) or _is_compound_of(b, a):
                s = max(s, ROOT_SIM)
            if npmi:
                s = max(s, float(npmi.get(f"{a}|{b}" if a < b else f"{b}|{a}", 0.0)))
            S[i, j] = S[j, i] = s
    np.fill_diagonal(S, 1.0)
    return S


# ── Culture ──────────────────────────────────────────────────────────────────

# Cultures whose catalogue travels with a language. A listener rarely wants a
# Greek station to slide into English-language music on genre alone, so every
# artist outside the SEED's culture is discounted for the whole station
# (CROSS_CULTURE), never walled off: once the scene is used up, the drift may
# continue into its genre neighbours abroad, if it has any. Per pair was not
# enough — one discounted step (laiko → a folk-pop act) and the station was
# free to run on into dance-pop.
REGIONAL_COUNTRIES = frozenset({"GR"})


def artist_culture(country: Optional[str], genres=(), names=(),
                   regional: frozenset = REGIONAL_COUNTRIES) -> Optional[str]:
    """The regional culture an artist belongs to, or None (the borderless
    majority). Any one signal is enough: the enrichment country, a scene tag
    ('laiko'), or names written in the culture's script — country is missing or
    wrong exactly for the local acts this protects."""
    c = (country or "").strip().upper()
    if c in regional:
        return c
    for tok in genres or ():
        cu = tag_culture(tok)
        if cu in regional:
            return cu
    cu = script_culture(*names)
    return cu if cu in regional else None


def _is_culture_word(tok) -> bool:
    return "".join(ch for ch in str(tok).lower() if ch.isalnum()) in CULTURE_WORDS


# ── The artist graph ─────────────────────────────────────────────────────────

CROSS_CULTURE = 0.35   # affinity multiplier for artists outside the seed's culture
COLLAB_SIM = 0.9       # artists credited together on a track
INFERRED = 0.8         # discount on an untagged artist's borrowed profile
DONORS = 5             # tagged artists an untagged one borrows from, by sound
BORROW_SHARE = 0.5     # a borrowed tag survives at ≥ this share of the strongest
CULTURE_MIN_LINKS = 2  # collaborators needed (and agreeing) to carry a culture over
CULTURE_PASSES = 2     # a feature of a feature


def _default_keys(name: str) -> frozenset:
    return frozenset({name.lower()}) if name else frozenset()


class ArtistGraph:
    """Artists, their soft genre profiles and the affinity between them.

    `meta[i]` describes track i (rows of `X_unit`): {'artist': the enrichment
    artist (album artist), 'credit': the track's own artist credit, 'genres':
    canonical tokens, 'country', 'names': (artist, album, title)}. Genres and
    country are artist-level. `credit_keys` splits a credit string into member
    keys, so collaborations and an act's solo/collab credits can be linked.

    An artist with no tags borrows a profile, at a discount since it is a guess:
    a provisional one from the tagged artists that sound most like it
    (preferring its own culture), then averaged with the profiles of the acts it
    is credited with, which outvote the sound. Collaborations are human
    evidence; sound alone filed Greek rappers who feature each other as
    shoegaze. A library with no tags at all falls back to pure acoustic affinity
    between artists."""

    def __init__(self, X_unit: np.ndarray, meta: Sequence[dict],
                 npmi: Optional[dict] = None,
                 credit_keys: Callable[[str], frozenset] = _default_keys):
        names =[m.get("artist") or m.get("credit") or f"#{i}" for i, m in enumerate(meta)]
        self.units: list[str] = list(dict.fromkeys(names))
        uidx = {u: k for k, u in enumerate(self.units)}
        U = len(self.units)
        self.unit_of = np.array([uidx[n] for n in names], dtype=np.int32)
        members: list[list[int]] = [[] for _ in range(U)]
        for i, u in enumerate(self.unit_of):
            members[u].append(i)
        self.tracks_of = [np.array(m, dtype=np.int32) for m in members]

        def first(idxs, key):
            return next((meta[i].get(key) for i in idxs if meta[i].get(key)), None)

        # Same act (shared member key) and collaboration links.
        unit_keys = [credit_keys(u) for u in self.units]
        by_key: dict[str, set] = defaultdict(set)
        for u, ks in enumerate(unit_keys):
            for k in ks:
                by_key[k].add(u)
        self.same_act: dict[int, set] = defaultdict(set)
        for us in by_key.values():
            for u in us:
                self.same_act[u] |= us - {u}
        self.collab: dict[int, set] = defaultdict(set)
        for i, m in enumerate(meta):
            touched = {v for k in credit_keys(m.get("credit") or "") for v in by_key.get(k, ())}
            touched.add(int(self.unit_of[i]))
            if len(touched) > 1:
                for u in touched:
                    self.collab[u] |= touched - {u}

        # Profiles and culture. A collab credit whose acts are in the library
        # ('Toquel, Light') is not an artist: its own enrichment row is a lookup
        # of the whole string, which MusicBrainz answers with whichever namesake
        # it finds (British prog rock for 'Light', the Hong Kong band for
        # 'Beyond'), and that row put Greek trap inside indie stations. It takes
        # its profile from its members instead (the inference below), unless
        # the user filled it in by hand. Its country stays: most Greek evidence
        # in the library sits on these rows (members fused when they agree), and
        # a wrong foreign country can't add a culture, only fail to.
        profiles = []
        culture: list[Optional[str]] = []
        pinned = np.zeros(U, dtype=bool)   # culture the user set by hand
        manual_u = np.zeros(U, dtype=bool)
        foreign_row = np.zeros(U, dtype=bool)   # the row names a country of its own
        for u in range(U):
            idxs = members[u]
            manual_u[u] = any(meta[i].get("manual") for i in idxs)
            derived = len(unit_keys[u]) > 1 and bool(self.same_act[u]) and not manual_u[u]
            genres = () if derived else (first(idxs, "genres") or ())
            country = first(idxs, "country")
            profiles.append(sorted({t for t in genres if not _is_culture_word(t)}))
            texts = [x for i in idxs for x in (meta[i].get("names") or ()) if x]
            culture.append(artist_culture(country, genres, texts))
            pinned[u] = manual_u[u] and bool(country)
            foreign_row[u] = bool(country) and not derived
        self.culture = self._spread_culture(culture, pinned)
        # Collaborators overturned the country the row gave: the row describes a
        # namesake, so its genres are the namesake's (Moose → shoegaze). Drop
        # them and let the collaborators place the artist like any untagged one.
        # A hand-set row is the user's word and is kept.
        self.namesake = np.array([
            foreign_row[u] and not manual_u[u] and self.culture[u] != culture[u]
            for u in range(U)
        ], dtype=bool)
        for u in np.flatnonzero(self.namesake):
            profiles[u] = []
        culture_arr = np.array([c or "" for c in self.culture])
        self._same_culture = {c: culture_arr == c for c in set(culture_arr)}

        # Artist centroids in sound, for borrowing a profile and the no-tags fallback.
        C = np.stack([X_unit[t].mean(axis=0) for t in self.tracks_of]) if U else np.zeros((0, X_unit.shape[1]))
        C /= np.maximum(np.linalg.norm(C, axis=1, keepdims=True), 1e-9)
        self._centroids = C.astype(np.float32)

        # Tag profiles.
        self.tags = sorted({t for p in profiles for t in p})
        tidx = {t: k for k, t in enumerate(self.tags)}
        W = np.zeros((U, len(self.tags)), dtype=np.float32)
        for u, p in enumerate(profiles):
            for t in p:
                W[u, tidx[t]] = 1.0
        tagged = W.sum(axis=1) > 0
        self.inferred = np.zeros(U, dtype=bool)
        self.has_tags = bool(tagged.any())
        if self.has_tags:
            untagged = np.flatnonzero(~tagged)
            for u in untagged:
                pool = tagged & self._same_culture[culture_arr[u]]
                if not pool.any():
                    pool = tagged
                cand = np.flatnonzero(pool)
                sims = self._centroids[cand] @ self._centroids[u]
                top = np.argsort(-sims)[:DONORS]
                w = np.maximum(sims[top], 0.0)
                if w.sum() > 0:
                    W[u] = (W[cand[top]] * w[:, None]).sum(axis=0) / w.sum()
            # Collaborators outvote the sound: each pass averages an untagged
            # artist's own guess with its credited acts' profiles, so evidence
            # travels along chains of features (two passes reach a feature of a
            # feature).
            for _ in range(2):
                nxt = W.copy()
                for u in untagged:
                    links = list(self.collab[u] | self.same_act[u])
                    if links:
                        nxt[u] = (W[links].sum(axis=0) + W[u]) / (len(links) + 1)
                W = nxt
            # Keep only what the donors largely agree on. A blend of five
            # artists half-matches everything: Trannos (Greek trap, no tags)
            # borrowed traces of house and pop and surfaced mid-way through a
            # dance-pop station.
            for u in untagged:
                top = W[u].max()
                if top > 0:
                    W[u] = np.where(W[u] >= BORROW_SHARE * top, W[u], 0.0)
            self.inferred[untagged] = W[untagged].sum(axis=1) > 0
        self.W = W
        # A tag most artists carry ('pop', 'electronic') says little about who
        # belongs together, so tags weigh by their rarity (smoothed IDF) in the
        # affinity. Measured on the device image: the acts that shared little
        # but 'pop' with Inna drop (Mazonakis, laiko-pop, 0.32 → 0.20; Fleetwood
        # Mac 0.24 → 0.19) while her dance-pop neighbours hold at ~0.65+.
        Wi = W * self._idf(W[tagged]) if self.has_tags else W
        S = tag_similarity(self.tags, npmi)
        self._Wi = Wi
        self._WS = Wi @ S
        self._norm = np.sqrt(np.maximum((self._WS * Wi).sum(axis=1), 0.0))
        self._rows: dict[int, np.ndarray] = {}

    def _spread_culture(self, culture: list, pinned: np.ndarray) -> list:
        """An artist most of whose collaborators share a regional culture
        belongs to it, whatever its own row says. The rows are wrong exactly
        here: a short Latin-script name ('Moose') matches a foreign namesake
        (a British shoegaze band) with full confidence, and the country fence
        then waves a Greek trapper into a Slowdive station. Only adds a culture,
        never removes one, and never overrides a country set by hand."""
        out = list(culture)
        for _ in range(CULTURE_PASSES):
            nxt = list(out)
            for u, c in enumerate(out):
                links = self.collab[u] | self.same_act[u]
                if c or pinned[u] or len(links) < CULTURE_MIN_LINKS:
                    continue
                votes = Counter(out[v] for v in links if out[v])
                if votes:
                    top, n = votes.most_common(1)[0]
                    if n >= CULTURE_MIN_LINKS and n > len(links) / 2:
                        nxt[u] = top
            out = nxt
        return out

    @staticmethod
    def _idf(W_tagged: np.ndarray) -> np.ndarray:
        n = len(W_tagged)
        df = (W_tagged > 0).sum(axis=0)
        return (np.log((1.0 + n) / (1.0 + df)) + 1.0).astype(np.float32)

    def __len__(self) -> int:
        return len(self.units)

    def row(self, u: int) -> np.ndarray:
        """Affinity of artist `u` to every artist, in [0, 1] (self = 1)."""
        r = self._rows.get(u)
        if r is not None:
            return r
        if self.has_tags:
            if self._norm[u] > 0:
                r = (self._Wi @ self._WS[u]) / (self._norm * self._norm[u] + 1e-9)
            else:
                r = np.zeros(len(self.units), dtype=np.float32)
            if self.inferred[u]:
                r = r * INFERRED
            r = np.where(self.inferred, r * INFERRED, r)
        else:
            # No genre knowledge at all: sound is the whole signal, rescaled
            # onto [0, 1] so MIN_AFFINITY doesn't cut off a library nobody
            # has tagged.
            r = (self._centroids @ self._centroids[u] + 1.0) / 2.0
        r = np.clip(r, 0.0, 1.0).astype(np.float32)
        for v in self.collab[u]:
            r[v] = max(r[v], COLLAB_SIM)
        for v in self.same_act[u]:
            r[v] = 1.0
        r[u] = 1.0
        self._rows[u] = r
        return r

    def foreign_to(self, u: int) -> np.ndarray:
        """Mask of the artists outside artist `u`'s culture."""
        return ~self._same_culture[self.culture[u] or ""]

    def flavour(self, units: Sequence[int]) -> Optional[str]:
        """Display name for the genre a run of artists shares most, or None."""
        if not self.has_tags or not len(units):
            return None
        weight = self.W[list(units)].sum(axis=0)
        if weight.max() <= 0:
            return None
        return genre_display_label(self.tags[int(np.argmax(weight))])


# ── Repeat caps ──────────────────────────────────────────────────────────────

class RollingCaps:
    """Repeat limits over the last `window` emitted tracks (0 = the whole run):
    at most `max_per_artist` per act and `max_per_album` per release. Artists
    are credit-key sets, so a collab counts against every act it names. A limit
    of 0 disables it."""

    def __init__(self, max_per_artist: int = 0, max_per_album: int = 0, window: int = 0):
        self.max_per_artist = max_per_artist
        self.max_per_album = max_per_album
        self._recent: Optional[deque] = deque() if window > 0 else None
        self._window = window
        self._artists: Counter = Counter()
        self._albums: Counter = Counter()

    def ok(self, keys, album) -> bool:
        keys = keys or ()
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


# ── The drift ────────────────────────────────────────────────────────────────

RECENT = 5             # artists the drift is anchored on besides the seed
DRIFT = 0.9            # the seed's share decays by this per track…
SEED_FLOOR = 0.25      # …down to this, so a station never forgets where it began
REPEL = 0.5            # pushes the ranking away from rejected artists' neighbours
REPEAT_DECAY = 0.6     # an artist queued k times in the last REPEAT_WINDOW: × this**k
REPEAT_WINDOW = 12
ACOUSTIC_W = 0.15      # how much the segue (from the previous track) can reorder artists
MIN_AFFINITY = 0.1     # below this nothing connects the artist to the station
# The segue: how smoothly a track follows the previous one is tempo and energy,
# not the full timbre vector. Neither carries genre (tempo explains < 1% of the
# genre variance on the device image; it scores below chance alone), but both
# are what an ear notices between two songs. Measured over 16 stations
# (2026-11-02) against cosine to the previous track: the typical tempo jump fell
# from ~17% to ~2% BPM, the energy jump from 0.59 to 0.35 SD, with 82% of the
# same artists. Tempo is octave-folded — 62 and 124 BPM share a pulse, and beat
# trackers report either. Tolerances are Gaussian widths.
SEGUE_TEMPO = 0.08     # log2 tempo, octave-folded (≈ 6% BPM)
SEGUE_ENERGY = 1.0     # energy, in library standard deviations
# The seed's umbrella: the artists within this affinity of the seed artist
# (after the culture discount). Measured on the device image (2026-11-02) it is
# the scene a listener would name — Inna's runs to the dance-pop and house acts
# (≥ 0.45) and stops short of synth-pop and new wave (≤ 0.33); Kendrick Lamar's
# is hip-hop; Fleetwood Mac's is blues and soft rock; a laiko seed's is laiko.
# A taxonomy can't draw it: the coarse 'Pop' family holds dance-pop and soft rock
# alike, and so does Wikidata's 'pop music'.
UMBRELLA = 0.4


class Drift:
    """A resumable station from one seed track over an `ArtistGraph`. Every
    `take(n)` continues where the last stopped and never emits a track twice.

    Each pick ranks artists by the drift reference — the seed artist's affinity
    weighted by SEED_FLOOR + (1 − SEED_FLOOR)·DRIFT^n after n tracks, the rest
    shared by the last RECENT artists queued — discounted outside the seed's
    culture and for artists queued recently, pushed away from rejected ones,
    plus a small segue term (how smoothly the artist's best track follows the
    previous one in tempo and energy, `_segue`). That track is the one that
    plays.

    `focused` keeps the station under the seed's umbrella (UMBRELLA): it moves
    around inside it and ends when the umbrella is played out. Otherwise the
    drift may carry it on to any artist above MIN_AFFINITY. Either way a take
    comes back short instead of jumping somewhere unrelated."""

    def __init__(self, graph: ArtistGraph, X_unit: np.ndarray, seed_idx: int, *,
                 exclude=(), artist_keys: Optional[Sequence] = None,
                 album_keys: Optional[Sequence] = None,
                 caps: Optional[RollingCaps] = None, focused: bool = False,
                 bpm: Optional[np.ndarray] = None, energy: Optional[np.ndarray] = None):
        self.g = graph
        self.X = X_unit
        n = len(graph.unit_of)
        bpm = np.zeros(n) if bpm is None else np.asarray(bpm, dtype=np.float64)
        energy = np.zeros(n) if energy is None else np.asarray(energy, dtype=np.float64)
        self._tempo = np.log2(np.maximum(bpm, 1.0))
        self._timed = bpm > 0
        sd = energy.std()
        self._energy = (energy - energy.mean()) / (sd if sd > 1e-9 else 1.0)
        self.artist_keys = artist_keys
        self.album_keys = album_keys
        self.caps = caps or RollingCaps()
        self.used = np.zeros(len(graph.unit_of), dtype=bool)
        self.exclude(exclude)
        self.used[seed_idx] = True
        self.seed_unit = int(graph.unit_of[seed_idx])
        self._foreign = graph.foreign_to(self.seed_unit)
        self._inside: Optional[np.ndarray] = None
        if focused:
            near = graph.row(self.seed_unit)
            self._inside = np.where(self._foreign, near * CROSS_CULTURE, near) >= UMBRELLA
        self._last = seed_idx
        self._n = 0
        self._recent: deque = deque([self.seed_unit], maxlen=RECENT)
        self._played: deque = deque([self.seed_unit], maxlen=REPEAT_WINDOW)
        self._rejected: set[int] = set()

    # ── public ──────────────────────────────────────────────────────────────
    def take(self, n: int) -> list[int]:
        out: list[int] = []
        while len(out) < n:
            j = self._pick()
            if j is None:
                break
            self._emit(j)
            out.append(j)
        return out

    def exclude(self, idxs) -> None:
        for j in idxs:
            self.used[int(j)] = True

    def accept(self, idx: int) -> None:
        """A track listened through: its artist pulls the drift."""
        if 0 <= idx < len(self.used):
            self._recent.append(int(self.g.unit_of[idx]))

    def reject(self, idxs) -> None:
        """Negative feedback: never emit these, and steer away from their
        artists' neighbourhood for the rest of the station."""
        for j in idxs:
            j = int(j)
            self.used[j] = True
            u = int(self.g.unit_of[j])
            if u != self.seed_unit:
                self._rejected.add(u)
            self._recent = deque((v for v in self._recent if v != u), maxlen=RECENT)

    def flavour(self) -> Optional[str]:
        return self.g.flavour(list(self._recent))

    # ── internals ───────────────────────────────────────────────────────────
    def _reference(self) -> np.ndarray:
        g = self.g
        w = SEED_FLOOR + (1.0 - SEED_FLOOR) * DRIFT ** self._n
        ref = w * g.row(self.seed_unit)
        if self._recent:
            ref = ref + (1.0 - w) * np.mean([g.row(u) for u in self._recent], axis=0)
        else:
            ref = ref / w
        ref = np.where(self._foreign, ref * CROSS_CULTURE, ref)
        if self._rejected:
            ref = ref - REPEL * np.max([g.row(u) for u in self._rejected], axis=0)
        for u, k in Counter(self._played).items():
            ref[u] *= REPEAT_DECAY ** k
        return ref

    def _keys(self, j: int):
        return self.artist_keys[j] if self.artist_keys is not None else frozenset()

    def _album(self, j: int):
        return self.album_keys[j] if self.album_keys is not None else None

    def _segue(self) -> np.ndarray:
        """How smoothly each track follows the previous one, in [0, 1]. A track
        with no tempo on either side falls back to its timbre cosine."""
        a = self._last
        dt = np.abs((self._tempo - self._tempo[a] + 0.5) % 1.0 - 0.5)
        de = self._energy - self._energy[a]
        s = np.exp(-0.5 * ((dt / SEGUE_TEMPO) ** 2 + (de / SEGUE_ENERGY) ** 2))
        if self._timed[a] and self._timed.all():
            return s
        timbre = (self.X @ self.X[a] + 1.0) / 2.0
        return np.where(self._timed & self._timed[a], s, timbre)

    def _track(self, u: int, segue: np.ndarray) -> Optional[int]:
        idx = self.g.tracks_of[u]
        idx = idx[~self.used[idx]]
        for j in idx[np.argsort(-segue[idx])]:
            j = int(j)
            if self.caps.ok(self._keys(j), self._album(j)):
                return j
        return None

    def _pick(self) -> Optional[int]:
        ref = self._reference()
        if self._inside is not None:
            # The umbrella is the gate; inside it the reference only orders.
            ref = np.where(self._inside, ref, -np.inf)
            floor = -np.inf
        else:
            floor = MIN_AFFINITY
        segue = self._segue()
        best, best_score = None, -np.inf
        for u in np.argsort(-ref):
            u = int(u)
            if ref[u] == -np.inf or ref[u] < floor or ref[u] + ACOUSTIC_W <= best_score:
                break
            j = self._track(u, segue)
            if j is not None and ref[u] + ACOUSTIC_W * segue[j] > best_score:
                best, best_score = j, float(ref[u] + ACOUSTIC_W * segue[j])
        return best

    def _emit(self, j: int) -> None:
        u = int(self.g.unit_of[j])
        self.used[j] = True
        self.caps.add(self._keys(j), self._album(j))
        self._last = j
        self._n += 1
        self._recent.append(u)
        self._played.append(u)
