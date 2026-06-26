"""Genre 'BLOSUM': an NPMI genre×genre similarity learned from co-occurrence.

Flat Jaccard treats 'soft rock' vs 'hard rock' as zero overlap — identical to
'soft rock' vs 'techno'. This module learns, from how often two genre tokens
co-tag the same artist, a bounded similarity (NPMI ∈ [0,1], self = 1) that gives
partial credit for *related* genres. The eval (tools/eval_metadata_fusion.py)
showed it strictly beats Jaccard in the walk's metadata gate.

Pure stdlib (math only), so it imports cheaply on the walk's hot path and
compiles for Android like the rest of the metadata layer. The model is a small
sparse dict — built once during graph generation, persisted, and loaded by the
walk (never rebuilt at query time).
"""

from __future__ import annotations

import math
from collections import defaultdict


def build_npmi_model(token_sets) -> dict:
    """Learn the NPMI model from artist-level genre token sets.

    Each set is one artist's genres (the co-occurrence context). Returns a sparse
    dict {"a|b": npmi} with a < b and npmi > 0; the diagonal (self-similarity 1)
    is implicit. Empty input → empty model (the walk then degrades to Dice)."""
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
        pab = cab / N
        pmi = math.log(pab / ((df[a] / N) * (df[b] / N)))
        denom = -math.log(pab)
        npmi = (pmi / denom) if denom > 0 else 0.0
        if npmi > 0:
            model[f"{a}|{b}"] = round(npmi, 4)  # a < b by construction
    return model


def _pair(a: str, b: str, model: dict) -> float:
    if a == b:
        return 1.0
    return model.get(f"{a}|{b}" if a < b else f"{b}|{a}", 0.0)


def soft_set_sim(A, B, model: dict) -> float:
    """Symmetric soft similarity of two genre token sets in [0,1].

    Each token takes its best match in the other set (self = 1, related = NPMI,
    unrelated = 0), averaged both ways. With an empty model this reduces to the
    Dice coefficient — i.e. exact-overlap behaviour — so it is always safe."""
    if not A or not B:
        return 0.0
    s1 = sum(max(_pair(a, b, model) for b in B) for a in A)
    s2 = sum(max(_pair(a, b, model) for a in A) for b in B)
    return (s1 + s2) / (len(A) + len(B))
