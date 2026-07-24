#!/usr/bin/env python3
"""
projection_diagnostic.py — is the graph's PCA projection distorting the feature
space, or is the weak genre separation inherent to the computed features?

Motivation
----------
The genre diagnostic measures silhouette in **Zr** — the space *after* the
Kaiser-truncated PCA in `track_graph.build_acoustic_edges`. A negative
silhouette there can mean two very different things:

  (a) the features genuinely don't separate genres (a feature problem), or
  (b) the features DO separate genres, but the linear PCA projection collapses
      that structure on its way to Zr (a *projection* problem).

Widening the analysis window and the 4-D Camelot change both touch (a). This
tool isolates (b). It reconstructs the EXACT pre-projection continuous space the
acoustic graph builds (z-scored, scalar-weighted, covariance-cleaved, with the
harmonic block held out for late fusion — identical to build_acoustic_edges),
then scores a battery of projection methods against the **unprojected full
space** as the reference:

  • full          identity on the continuous block — what the features contain
  • pca_kaiser    Kaiser λ>1 (min 3 comps)         — what the graph ACTUALLY uses
  • pca_k{2,3,5,10,20}   fixed component counts     — the variance/structure curve
  • pca_whiten    Kaiser PCA, unit-variance axes    — removes PCA's variance scaling
  • zca_whiten    full-rank whitening               — equalises the metric, no rank loss
  • rand_gauss    Gaussian random projection (JL)   — control: does PCA beat a random map?
  • kpca_rbf      RBF kernel PCA                     — recovers nonlinear structure?
  • isomap        geodesic kNN + classical MDS      — recovers a curved manifold?

Every method is scored with both label-aware and label-free statistics:

  • knn_genre_purity@k + a label-PERMUTATION null (z above chance)
  • global & per-genre silhouette (same formula as plot_genre_report)
  • trustworthiness@k / continuity@k vs the full space  — the DISTORTION the
    projection introduces (neighbours invented / neighbours lost)
  • Shepard rank corr of pairwise distances (full vs projection) — global distortion
  • variance retained (PCA family)

Reading it
----------
  • full-space purity ≈ null            → features carry no (linear) genre signal;
                                          the projection is NOT the bottleneck.
  • full-space purity ≫ pca_kaiser      → PCA is discarding genre signal → keep
                                          more components / whiten / go nonlinear.
  • a nonlinear method ≫ best linear    → genre structure is curved; a linear PCA
                                          cannot preserve it by construction.

Pure NumPy — no sklearn/scipy, matching the on-device DSP philosophy, and
CPU-light (a single O(N^2) eigendecomp at N~1.1k, no iterative solvers) so it
will not thermally throttle.

Usage
-----
    python tools/projection_diagnostic.py [--db PATH] [--out DIR] [--knn 10]
    python tools/projection_diagnostic.py --db tools/offload_cache/bundle/library.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import numpy as np

# Reuse the app's real cleaving + bucketing so the numbers are comparable to the
# in-app genre_report. pca_engine/harmonic import only numpy (no flet/config).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))
from utils.pca_engine import redundant_raw_features, genre_bucket  # noqa: E402
from utils.harmonic import key_index_to_camelot  # noqa: E402
from utils.genre_eval import knn_purity, knn_purity_z, compute_silhouette_scores  # noqa: E402

# Layout constants mirror dsp.py. N_MFCC=20, N_CHROMA=12.
_N_MFCC = 20
_N_CHROMA = 12

# The 7 raw continuous scalars subject to covariance cleaving. Matches
# pca_engine._RAW_FEATURES exactly (key/`key_mode` is no longer part of the
# geometry, so redundant_raw_features never returns it).
_CONT_SCALARS = [
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast",
]

# Graph build defaults (build_acoustic_edges signature).
SCALAR_WEIGHT = 1.5
HARMONIC_WEIGHT = 1.5


# ── Data loading ─────────────────────────────────────────────────────────────


def load_rows(db_path: str) -> list[dict]:
    """Load every track with a timbre BLOB, plus its genre when available.

    Works against either the bundle `library.db` (timbre in play_counts, genre
    via tracks→albums) or a standalone `feature_cache.db` (no genre).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    if "play_counts" in tables:
        sql = """
            SELECT pc.track_path AS path, pc.bpm, pc.brightness, pc.energy,
                   pc.rolloff, pc.beat_strength, pc.spectral_flatness,
                   pc.spectral_contrast, pc.key_index, pc.timbre,
                   al.genre AS genre
            FROM play_counts pc
            LEFT JOIN tracks t  ON t.path = pc.track_path
            LEFT JOIN albums al ON al.id  = t.album_id
            WHERE pc.timbre IS NOT NULL
        """
    else:
        sql = """
            SELECT track_path AS path, bpm, brightness, energy, rolloff,
                   beat_strength, spectral_flatness, spectral_contrast,
                   key_index, timbre, NULL AS genre
            FROM feature_cache
            WHERE timbre IS NOT NULL
        """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return rows


def _graph_timbre(blob: bytes) -> np.ndarray | None:
    """Version-aware graph timbre slice (the BLOB minus the mfcc_delta block),
    mirroring dsp.unpack_graph_embedding for v4 and the v3 layout on disk.

      v4 (88 floats): [mean20|std20|delta20|chroma12|rhythm16] → drop delta → 68
      v3 (52 floats): [mean20|delta20|chroma12]                → drop delta → 32
    """
    n = len(blob) // 4
    v = np.frombuffer(blob, dtype="<f4").astype(np.float32)
    if n == 88:
        return np.delete(v, np.s_[2 * _N_MFCC:3 * _N_MFCC])      # 68
    if n == 52:
        return np.concatenate([v[0:_N_MFCC], v[2 * _N_MFCC:]])   # mean + chroma = 32
    return None


def _harmonic_coords(key_index: int) -> tuple[float, float, float]:
    cam = key_index_to_camelot(key_index or 0)
    if cam is None:
        return 0.0, 0.0, 0.0
    hour, ring = cam
    theta = 2.0 * np.pi * (hour - 1) / 12.0
    return float(np.cos(theta)), float(np.sin(theta)), (1.0 if ring == "B" else 0.0)


def build_spaces(rows: list[dict]):
    """Reconstruct the continuous pre-projection block Z_cont and the late-fused
    harmonic block H exactly as track_graph.build_acoustic_edges does.

    Returns (Z_cont, H, genres, blob_floats, surviving_scalars).
    """
    redundant = redundant_raw_features(rows)
    surviving = [s for s in _CONT_SCALARS if s not in redundant]

    timbre, cont_scalars, harm, genres = [], [], [], []
    blob_floats = None
    for r in rows:
        t = _graph_timbre(r["timbre"])
        if t is None:
            continue
        blob_floats = len(r["timbre"]) // 4
        bpm = float(r["bpm"] or 0.0)
        sc = {
            "bpm": float(np.log2(max(bpm, 1.0))),
            "brightness": float(r["brightness"] or 0.0),
            "energy": float(r["energy"] or 0.0),
            "rolloff": float(r["rolloff"] or 0.0),
            "beat_strength": float(r["beat_strength"] or 0.0),
            "spectral_flatness": float(r["spectral_flatness"] or 0.0),
            "spectral_contrast": float(r["spectral_contrast"] or 0.0),
        }
        timbre.append(t)
        cont_scalars.append([sc[s] for s in surviving])
        harm.append(_harmonic_coords(int(r["key_index"] or 0)))
        genres.append(r.get("genre"))

    T = np.asarray(timbre, dtype=np.float64)
    S = np.asarray(cont_scalars, dtype=np.float64).reshape(len(timbre), -1)
    H_raw = np.asarray(harm, dtype=np.float64)

    # z-score continuous block (timbre + surviving scalars) column-wise, then
    # boost the scalar tail by scalar_weight — identical to build_acoustic_edges.
    Xc = np.hstack([T, S])
    Xc = _zscore(Xc)
    if S.shape[1]:
        Xc[:, T.shape[1]:] *= SCALAR_WEIGHT

    # z-score the harmonic block separately and apply its own weight (late fusion).
    H = _zscore(H_raw) * HARMONIC_WEIGHT
    return Xc, H, genres, blob_floats, surviving


def _zscore(X: np.ndarray) -> np.ndarray:
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd = np.where(sd < 1e-8, 1.0, sd)
    return (X - mu) / sd


# ── Projection methods (each maps the continuous block Z → (N, k)) ───────────


def _svd_pca(Z, k=None, whiten=False):
    Zc = Z - Z.mean(axis=0)
    U, s, Vt = np.linalg.svd(Zc, full_matrices=False)
    ev = (s ** 2) / (Z.shape[0] - 1)
    if k is None:
        k = max(3, int((ev > 1.0).sum()))
    k = min(k, Vt.shape[0])
    comp = Zc @ Vt[:k].T
    if whiten:
        comp = comp / np.sqrt(ev[:k] + 1e-12)
    var_ret = float(ev[:k].sum() / ev.sum()) if ev.sum() > 0 else 0.0
    return comp.astype(np.float64), k, var_ret


def _zca_whiten(Z):
    Zc = Z - Z.mean(axis=0)
    U, s, Vt = np.linalg.svd(Zc, full_matrices=False)
    ev = (s ** 2) / (Z.shape[0] - 1)
    W = Vt.T @ np.diag(1.0 / np.sqrt(ev + 1e-12)) @ Vt   # ZCA: rotate back
    return (Zc @ W).astype(np.float64)


def _random_projection(Z, k, seed=0):
    rng = np.random.RandomState(seed)
    R = rng.normal(0.0, 1.0 / np.sqrt(k), size=(Z.shape[1], k))
    return (Z @ R).astype(np.float64)


def _kernel_pca_rbf(Z, k):
    """RBF kernel PCA. gamma via the median pairwise-distance heuristic."""
    D2 = _sq_dists(Z)
    med = np.median(D2[D2 > 0])
    gamma = 1.0 / (med + 1e-12)
    K = np.exp(-gamma * D2)
    N = K.shape[0]
    one = np.ones((N, N)) / N
    Kc = K - one @ K - K @ one + one @ K @ one          # centre in feature space
    Kc = (Kc + Kc.T) / 2.0
    vals, vecs = np.linalg.eigh(Kc)
    idx = np.argsort(vals)[::-1][:k]
    vals, vecs = vals[idx], vecs[:, idx]
    vals = np.maximum(vals, 1e-12)
    return (vecs * np.sqrt(vals)).astype(np.float64)


def _isomap(Z, k, n_neighbors=15):
    """Classical Isomap: kNN graph → all-pairs geodesics → classical MDS."""
    D2 = _sq_dists(Z)
    D = np.sqrt(np.maximum(D2, 0.0))
    N = D.shape[0]
    # kNN graph (symmetric): keep each node's n_neighbors nearest.
    nn = np.argsort(D, axis=1)[:, 1:n_neighbors + 1]
    G = np.full((N, N), np.inf)
    np.fill_diagonal(G, 0.0)
    for i in range(N):
        for j in nn[i]:
            G[i, j] = G[j, i] = D[i, j]
    geo = _all_pairs_dijkstra(G)
    # Disconnected pairs → large finite (1.5× the largest finite geodesic).
    finite = geo[np.isfinite(geo)]
    geo[~np.isfinite(geo)] = (finite.max() * 1.5) if finite.size else 1.0
    # Classical MDS on squared geodesics.
    G2 = geo ** 2
    J = np.eye(N) - np.ones((N, N)) / N
    B = -0.5 * J @ G2 @ J
    B = (B + B.T) / 2.0
    vals, vecs = np.linalg.eigh(B)
    idx = np.argsort(vals)[::-1][:k]
    vals, vecs = np.maximum(vals[idx], 1e-12), vecs[:, idx]
    return (vecs * np.sqrt(vals)).astype(np.float64)


def _all_pairs_dijkstra(G):
    """All-pairs shortest paths on a sparse symmetric weight matrix (inf = no
    edge). Pure-numpy Dijkstra per source; fine at N~1.1k."""
    import heapq
    N = G.shape[0]
    adj = [np.where(np.isfinite(G[i]))[0] for i in range(N)]
    out = np.full((N, N), np.inf)
    for src in range(N):
        dist = out[src]
        dist[src] = 0.0
        pq = [(0.0, src)]
        seen = np.zeros(N, dtype=bool)
        while pq:
            d, u = heapq.heappop(pq)
            if seen[u]:
                continue
            seen[u] = True
            for v in adj[u]:
                nd = d + G[u, v]
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
    return out


# ── Geometry helpers ─────────────────────────────────────────────────────────


def _sq_dists(X):
    sq = (X ** 2).sum(1)
    D2 = sq[:, None] - 2.0 * X @ X.T + sq[None, :]
    return np.maximum(D2, 0.0)


def _ranks_from_dist(D2):
    """Rank matrix R[i,j] = position of j in i's ascending distance order
    (self = 0). Used by trustworthiness / continuity."""
    order = np.argsort(D2, axis=1)
    N = D2.shape[0]
    R = np.empty((N, N), dtype=np.int32)
    rows = np.arange(N)[:, None]
    R[rows, order] = np.arange(N)[None, :]
    return R, order


# ── Metrics ──────────────────────────────────────────────────────────────────


def silhouette(D, labels, valid):
    """Per-point silhouette in distance matrix D over labelled points only
    (same a/b formula as plot_genre_report). Returns (global_mean, {bucket:mean})."""
    _, glob, sils = compute_silhouette_scores(D, labels, valid)
    return glob, sils


def knn_purity_null(order, labels, valid, k, B=200, seed=0):
    """Label-permutation null for knn_purity: shuffle bucket labels B times,
    return (null_mean, null_std). The z-score (purity-mean)/std says whether the
    observed purity is real structure or chance given the bucket sizes."""
    _, null_mean, null_std, _ = knn_purity_z(order, labels, valid, k, B, seed)
    return null_mean, null_std


def trustworthiness(R_full, order_proj, k):
    """Venna & Kaski trustworthiness@k: penalises points that are k-NN in the
    PROJECTION but far in the FULL space (invented neighbours)."""
    N = R_full.shape[0]
    total = 0.0
    for i in range(N):
        proj_k = [j for j in order_proj[i] if j != i][:k]
        for j in proj_k:
            r = R_full[i, j]                # rank in full space (self=0)
            if r > k:
                total += (r - k)
    norm = 2.0 / (N * k * (2 * N - 3 * k - 1))
    return 1.0 - norm * total


def shepard_spearman(D_full, D_proj, sample=40000, seed=0):
    """Spearman rank correlation between full-space and projected pairwise
    distances over a random sample of pairs (global distortion)."""
    N = D_full.shape[0]
    rng = np.random.RandomState(seed)
    iu = np.triu_indices(N, k=1)
    m = iu[0].size
    if m > sample:
        pick = rng.choice(m, sample, replace=False)
        a = D_full[iu[0][pick], iu[1][pick]]
        b = D_proj[iu[0][pick], iu[1][pick]]
    else:
        a = D_full[iu]
        b = D_proj[iu]
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    ra = (ra - ra.mean()) / (ra.std() + 1e-12)
    rb = (rb - rb.mean()) / (rb.std() + 1e-12)
    return float((ra * rb).mean())


# ── Driver ───────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tools/offload_cache/bundle/library.db")
    ap.add_argument("--out", default="pca_genre_report/projection_diagnostic")
    ap.add_argument("--knn", type=int, default=10)
    ap.add_argument("--null-B", type=int, default=200)
    ap.add_argument("--no-isomap", action="store_true")
    args = ap.parse_args()

    rows = load_rows(args.db)
    Zc, H, genres, blob_floats, surviving = build_spaces(rows)
    N, Dc = Zc.shape
    ver = {88: "v4", 52: "v3"}.get(blob_floats, f"{blob_floats}f")
    labels = np.array([str(genre_bucket(g)) for g in genres], dtype=object)
    valid = np.array([g not in ("Unknown",) and gv not in (None, "")
                      for g, gv in zip(labels, genres)])

    print(f"\nLoaded {N} tracks | timbre={ver} ({blob_floats}f) | "
          f"continuous block Dc={Dc} (timbre+{len(surviving)} scalars: {surviving}) "
          f"| harmonic H={H.shape[1]} | labelled={int(valid.sum())}")
    bcounts = {b: int((labels[valid] == b).sum()) for b in sorted(set(labels[valid]))}
    print("bucket sizes:", bcounts)

    kaiser = _svd_pca(Zc)[1]
    print(f"Kaiser k (app's live component count) = {kaiser}\n")

    # Build every projection of the continuous block, then late-fuse H so the
    # comparison differs ONLY in how the continuous block was projected.
    methods = {
        "full":        (Zc, None),
        "pca_kaiser":  (_svd_pca(Zc)[0], _svd_pca(Zc)[2]),
        "pca_2":       (_svd_pca(Zc, 2)[0], _svd_pca(Zc, 2)[2]),
        "pca_3":       (_svd_pca(Zc, 3)[0], _svd_pca(Zc, 3)[2]),
        "pca_5":       (_svd_pca(Zc, 5)[0], _svd_pca(Zc, 5)[2]),
        "pca_10":      (_svd_pca(Zc, 10)[0], _svd_pca(Zc, 10)[2]),
        "pca_20":      (_svd_pca(Zc, 20)[0], _svd_pca(Zc, 20)[2]),
        "pca_whiten":  (_svd_pca(Zc, kaiser, whiten=True)[0], None),
        "zca_whiten":  (_zca_whiten(Zc), None),
        "rand_gauss":  (_random_projection(Zc, kaiser), None),
        "kpca_rbf":    (_kernel_pca_rbf(Zc, max(kaiser, 10)), None),
    }
    if not args.no_isomap:
        print("computing isomap (geodesic kNN + MDS)…", flush=True)
        methods["isomap"] = (_isomap(Zc, max(kaiser, 10)), None)

    # Reference full space (continuous identity ⊕ harmonic) for distortion metrics.
    Y_full = np.hstack([Zc, H])
    D_full = np.sqrt(_sq_dists(Y_full))
    R_full, order_full = _ranks_from_dist(D_full ** 2)

    null_mean, null_std = None, None

    results = []
    for name, (proj, var_ret) in methods.items():
        Y = np.hstack([proj, H])
        D2 = _sq_dists(Y)
        D = np.sqrt(D2)
        R, order = _ranks_from_dist(D2)

        sil_g, sil_per = silhouette(D, labels, valid)
        pur, pur_per = knn_purity(order, labels, valid, args.knn)
        if null_mean is None:
            null_mean, null_std = knn_purity_null(
                order, labels, valid, args.knn, B=args.null_B)
        z = (pur - null_mean) / null_std
        # trustworthiness@k: neighbours invented by the projection (far in full).
        # continuity@k: neighbours lost by the projection (= trust with roles swapped).
        trust = trustworthiness(R_full, order, args.knn)
        cont = trustworthiness(R, order_full, args.knn)
        shep = shepard_spearman(D_full, D)

        results.append(dict(
            name=name, dim=Y.shape[1], var=var_ret, sil=sil_g, pur=pur, z=z,
            trust=trust, cont=cont, shep=shep, sil_per=sil_per, pur_per=pur_per,
        ))
        print(f"  scored {name:12s} dim={Y.shape[1]:3d} "
              f"purity={pur:.3f} (z={z:+.1f}) sil={sil_g:+.3f} "
              f"trust={trust:.3f} cont={cont:.3f} shepard={shep:+.3f}")

    _write_report(args, rows, ver, blob_floats, N, Dc, surviving, H.shape[1],
                  bcounts, kaiser, null_mean, null_std, results)


def _write_report(args, rows, ver, blob_floats, N, Dc, surviving, Hdim,
                  bcounts, kaiser, null_mean, null_std, results):
    os.makedirs(args.out, exist_ok=True)
    L = []
    L.append("PROJECTION DISTORTION DIAGNOSTIC")
    L.append("=" * 78)
    L.append(f"db={args.db}")
    L.append(f"tracks={N}  timbre={ver}({blob_floats}f)  "
             f"continuous_block_dim={Dc} (timbre + {len(surviving)} scalars)  "
             f"harmonic_dim={Hdim}")
    L.append(f"surviving scalars (post covariance-cleave): {surviving}")
    L.append(f"genre buckets: {bcounts}")
    L.append(f"Kaiser k (the app's live PCA component count) = {kaiser}")
    L.append(f"knn={args.knn}  label-permutation null purity = "
             f"{null_mean:.3f} ± {null_std:.3f}  (chance baseline)")
    L.append("")
    L.append("Columns:")
    L.append("  purity   mean genre-purity of the k nearest neighbours (↑ better)")
    L.append("  z        (purity − null)/null_std — SDs above chance (↑ better)")
    L.append("  sil      global genre silhouette (↑ better; >0 = own regions)")
    L.append("  trust    trustworthiness@k vs full space (invented neighbours; 1=none)")
    L.append("  cont     continuity@k vs full space (lost neighbours; 1=none)")
    L.append("  shepard  Spearman of pairwise distances vs full space (1=undistorted)")
    L.append("  var      variance retained (PCA family only)")
    L.append("")
    hdr = (f"{'method':12s} {'dim':>4s} {'purity':>7s} {'z':>6s} {'sil':>7s} "
           f"{'trust':>6s} {'cont':>6s} {'shep':>6s} {'var':>6s}")
    L.append(hdr)
    L.append("-" * len(hdr))
    for r in sorted(results, key=lambda x: -x["pur"]):
        var = f"{r['var']:.2f}" if r["var"] is not None else "  -"
        L.append(f"{r['name']:12s} {r['dim']:4d} {r['pur']:7.3f} {r['z']:+6.1f} "
                 f"{r['sil']:+7.3f} {r['trust']:6.3f} {r['cont']:6.3f} "
                 f"{r['shep']:+6.3f} {var:>6s}")
    L.append("")

    # Per-genre purity for full vs the app's projection — shows WHICH genres the
    # projection costs.
    full = next(r for r in results if r["name"] == "full")
    pk = next(r for r in results if r["name"] == "pca_kaiser")
    L.append("per-genre kNN purity: full space → pca_kaiser (Δ = projection cost)")
    allg = sorted(set(full["pur_per"]) | set(pk["pur_per"]),
                  key=lambda g: -full["pur_per"].get(g, (0, 0))[1])
    for g in allg:
        fp, n = full["pur_per"].get(g, (0.0, 0))
        kp, _ = pk["pur_per"].get(g, (0.0, 0))
        L.append(f"  {g:12s} n={n:4d}  {fp:.3f} → {kp:.3f}  (Δ={kp - fp:+.3f})")
    L.append("")

    # Programmatic verdict.
    best = max(results, key=lambda x: x["pur"])
    L.append("VERDICT")
    L.append("-" * 78)
    if full["z"] < 2.0:
        L.append(f"• Full-space purity z={full['z']:+.1f} is at/near chance: the "
                 "computed features carry little linear genre signal. The PCA "
                 "projection is NOT the bottleneck — this is a feature problem, "
                 "not a projection problem.")
    else:
        gap = full["pur"] - pk["pur"]
        L.append(f"• Features DO carry genre signal (full z={full['z']:+.1f}).")
        if gap > 0.02:
            L.append(f"• pca_kaiser loses {gap:+.3f} purity vs full space → the "
                     "Kaiser truncation is discarding genre structure. Keep more "
                     "components or whiten.")
        else:
            L.append("• pca_kaiser preserves nearly all of the full-space purity "
                     "→ truncation is not the problem.")
    nl = [r for r in results if r["name"] in ("kpca_rbf", "isomap")]
    lin_best = max((r for r in results if r["name"].startswith("pca")
                    or r["name"] in ("full", "zca_whiten", "rand_gauss")),
                   key=lambda x: x["pur"])
    for r in nl:
        if r["pur"] - lin_best["pur"] > 0.02:
            L.append(f"• {r['name']} beats the best linear map by "
                     f"{r['pur'] - lin_best['pur']:+.3f} → genre structure is "
                     "nonlinear; a linear PCA cannot preserve it by construction.")
    rg = next(r for r in results if r["name"] == "rand_gauss")
    L.append(f"• Best method overall: {best['name']} (purity={best['pur']:.3f}, "
             f"z={best['z']:+.1f}).")
    L.append(f"• Random-projection control purity={rg['pur']:.3f} "
             f"(z={rg['z']:+.1f}) — any method not clearly above this adds nothing "
             "over a structure-blind linear map.")

    report = "\n".join(L)
    path = os.path.join(args.out, "projection_diagnostic.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print("\n" + report)
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
