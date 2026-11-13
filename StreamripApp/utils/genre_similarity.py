"""Genre 'BLOSUM': an NPMI genre×genre similarity learned from co-occurrence.

Flat Jaccard treats 'soft rock' vs 'hard rock' as zero overlap — identical to
'soft rock' vs 'techno'. This module learns, from how often two genre tokens
co-tag the same artist, a bounded similarity (NPMI ∈ [0,1], self = 1) that gives
partial credit for *related* genres. Stations read it as one of the human
sources of tag-to-tag similarity (`artist_graph.tag_similarity`).

Pure stdlib (math only), so it imports cheaply on the walk's hot path and
compiles for Android like the rest of the metadata layer. The model is a small
sparse dict — built once during graph generation, persisted, and loaded by the
stations (never rebuilt at query time).
"""

from __future__ import annotations

import math
from collections import defaultdict


# Evidence gating for the NPMI estimate. Raw NPMI saturates at exactly 1.0 —
# i.e. "these two genres are the SAME genre" — whenever two tokens always appear
# together, *regardless of how little evidence there is*: two tags seen on one
# single artist give pab = pa = pb = 1/N, so pmi = log N, denom = log N, npmi = 1.
# On a personal library that is the common case, not the exception (measured on
# the real library: 68% of pairs rested on ONE artist and 87 pairs sat at 1.0,
# 8 of them bridging different coarse families — 'countryrock ≡ swamprock',
# 'afrohouse ≡ worldbeat'). Those phantom equivalences are exactly the low-
# evidence bridges the walk's veto then waves through.
#
# Two guards, both on *support* (the number of artists attesting a pair):
#   • MIN_SUPPORT — a pair seen on a single artist is an anecdote, not a
#     relation; drop it entirely.
#   • SHRINKAGE_K — scale the estimate by cab/(cab+k) so similarity has to be
#     EARNED with repeated evidence. A pair on 2 artists keeps 2/7 of its raw
#     NPMI, on 10 artists 2/3, and only asymptotically reaches it. This is the
#     standard empirical-Bayes shrink toward the "unrelated" prior.
DEFAULT_MIN_SUPPORT = 2
DEFAULT_SHRINKAGE_K = 5.0


def build_npmi_model(
    token_sets,
    min_support: int = DEFAULT_MIN_SUPPORT,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> dict:
    """Learn the NPMI model from artist-level genre token sets.

    Each set is one artist's genres (the co-occurrence context). Returns a sparse
    dict {"a|b": npmi} with a < b and npmi > 0; the diagonal (self-similarity 1)
    is implicit. Empty input → empty model (the walk then degrades to Dice).

    The stored value is the support-shrunk NPMI  npmi · cab/(cab+shrinkage_k),
    with pairs below `min_support` attesting artists dropped outright — see the
    constants above for why raw NPMI is unusable on a library-sized corpus.
    Pass min_support=1, shrinkage_k=0 to recover the raw estimate (used by the
    A/B harness)."""
    docs = [set(s) for s in token_sets if s]
    N = len(docs)
    model: dict = {}
    if N == 0:
        return model

    df: dict = defaultdict(int)         # artists containing token
    co: dict = defaultdict(int)         # artists containing both tokens
    for s in docs:
        toks = sorted(s)
        for a in toks:
            df[a] += 1
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                co[(toks[i], toks[j])] += 1

    for (a, b), cab in co.items():
        if cab < min_support:
            continue
        pab = cab / N
        pmi = math.log(pab / ((df[a] / N) * (df[b] / N)))
        denom = -math.log(pab)
        npmi = (pmi / denom) if denom > 0 else 0.0
        if npmi <= 0:
            continue
        if shrinkage_k > 0:
            npmi *= cab / (cab + shrinkage_k)
        if npmi > 0:
            model[f"{a}|{b}"] = round(npmi, 4)  # a < b by construction
    return model
