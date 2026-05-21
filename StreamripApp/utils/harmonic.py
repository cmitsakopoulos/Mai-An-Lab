"""Camelot-wheel helpers for harmonic-mixing-aware playlist sequencing.

The DSP layer estimates a track's key_index in [0, 23]:
    0..11  → C, C#/Db, D, D#/Eb, E, F, F#/Gb, G, G#/Ab, A, A#/Bb, B major
    12..23 → same pitch classes, minor

The Camelot wheel is the DJ industry's de-facto mapping for "compatible"
keys: two tracks are harmonically adjacent when their Camelot codes differ
by ±1 hour (perfect-fifth move), share an hour with opposite mode (relative
major/minor), or match exactly (same key). This module exposes a single
cost helper used by the playlist sequencer.

Camelot table (hour, mode):
    1A  = A♭ minor   1B  = B  major
    2A  = E♭ minor   2B  = F♯ major
    3A  = B♭ minor   3B  = D♭ major
    4A  = F  minor   4B  = A♭ major
    5A  = C  minor   5B  = E♭ major
    6A  = G  minor   6B  = B♭ major
    7A  = D  minor   7B  = F  major
    8A  = A  minor   8B  = C  major
    9A  = E  minor   9B  = G  major
   10A  = B  minor  10B  = D  major
   11A  = F♯ minor  11B  = A  major
   12A  = D♭ minor  12B  = E  major

The mapping below is derived from the standard Camelot wheel published by
Mixed In Key; consult their docs for the canonical reference.
"""

from __future__ import annotations

# Pitch-class index → Camelot hour for the major (B) ring.
# pc 0 = C major = 8B → hour 8.
_MAJOR_HOUR = {
    0: 8,   # C
    1: 3,   # C#/Db
    2: 10,  # D
    3: 5,   # D#/Eb
    4: 12,  # E
    5: 7,   # F
    6: 2,   # F#/Gb
    7: 9,   # G
    8: 4,   # G#/Ab
    9: 11,  # A
    10: 6,  # A#/Bb
    11: 1,  # B
}

# Pitch-class index → Camelot hour for the minor (A) ring.
# A minor = 8A → hour 8.
_MINOR_HOUR = {
    0: 5,   # C minor
    1: 12,  # C# minor (= Db minor)
    2: 7,   # D minor
    3: 2,   # D# minor
    4: 9,   # E minor
    5: 4,   # F minor
    6: 11,  # F# minor
    7: 6,   # G minor
    8: 1,   # G# minor (= Ab minor)
    9: 8,   # A minor
    10: 3,  # A# minor (= Bb minor)
    11: 10, # B minor
}


def key_index_to_camelot(key_index: int) -> tuple[int, str] | None:
    """Returns (hour, ring) or None for an out-of-range key_index.
    ring is 'A' (minor) or 'B' (major)."""
    if key_index is None:
        return None
    k = int(key_index)
    if k < 0 or k > 23:
        return None
    if k < 12:
        return _MAJOR_HOUR[k], "B"
    return _MINOR_HOUR[k - 12], "A"


def camelot_distance(a: int, b: int) -> int:
    """Symmetric distance between two key_indexes, normalised so musically
    adjacent keys (same key, ±1 hour same ring, relative major/minor) return
    0 and the worst transition returns ~6.

    Distances:
        0 — identical key, or relative major/minor (same hour, opposite ring)
        1 — adjacent hour, same ring (perfect 4th/5th move)
        2..6 — increasingly distant.

    Returns 6 (max) if either key is unknown so unknown-key tracks aren't
    preferred as anchors in a harmonic walk."""
    ca = key_index_to_camelot(a)
    cb = key_index_to_camelot(b)
    if ca is None or cb is None:
        return 6
    hour_a, ring_a = ca
    hour_b, ring_b = cb
    # Circular distance on 12-hour wheel.
    diff = abs(hour_a - hour_b)
    hour_dist = min(diff, 12 - diff)
    if ring_a == ring_b:
        # Same ring: distance = hours apart.
        return hour_dist
    # Opposite ring: relative major/minor (same hour) is the only zero-cost
    # cross. Any other cross-ring transition adds one to the hour distance,
    # capped at 6 to keep the range [0, 6] like the same-ring case.
    if hour_dist == 0:
        return 0
    return min(hour_dist + 1, 6)


def camelot_penalty(a: int, b: int) -> float:
    """Normalised harmonic penalty in [0, 1]. 0 means perfectly compatible
    (same key or relative major/minor); 1 means maximally clashing."""
    return camelot_distance(a, b) / 6.0


def matches_mode_preference(key_index: int, pref: str | None) -> bool:
    """True if the track's key_index is in the requested mode ('major' /
    'minor'), or pref is None. Used to pick a mood-appropriate anchor."""
    if not pref:
        return True
    ca = key_index_to_camelot(key_index)
    if ca is None:
        return False
    _, ring = ca
    if pref == "major":
        return ring == "B"
    if pref == "minor":
        return ring == "A"
    return True
