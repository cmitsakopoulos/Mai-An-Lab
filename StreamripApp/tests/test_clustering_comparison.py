"""Clustering comparison benchmark script.

Invoked manually:
    python3 tests/test_clustering_comparison.py
"""

from __future__ import annotations

import asyncio
import glob
import os
import sys
import tempfile
import zipfile
import shutil
import time
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Override APP_DIR so we don't pollute persistent state
from utils import config
config.APP_DIR = tempfile.mkdtemp(prefix="test_clustering_comp_")

from utils import track_graph as tg
from utils.harmonic import key_index_to_camelot
from utils.db_manager import DatabaseManager


def _resolve_bundle() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = sorted(
        glob.glob(os.path.join(here, "..", "..", "tools", "analyzed_states", "*.analysed.zip")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("No .analysed.zip found under tools/analyzed_states/")
    return candidates[0]


def _extract(zip_path: str) -> str:
    out = tempfile.mkdtemp(prefix="test_state_")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    return out


def _camelot_str(ki: int) -> str:
    cam = key_index_to_camelot(ki)
    if cam is None:
        return "?"
    hour, ring = cam
    return f"{hour}{ring}"


def camelot_distance(ki1: int, ki2: int) -> float:
    cam1 = key_index_to_camelot(ki1)
    cam2 = key_index_to_camelot(ki2)
    if cam1 is None or cam2 is None:
        return 6.0
    h1, r1 = cam1
    h2, r2 = cam2
    dist_h = min(abs(h1 - h2), 12 - abs(h1 - h2))
    dist_r = 0.0 if r1 == r2 else 1.0
    return float(dist_h + dist_r)


def _spectral_cluster(
    candidates: list[list[tuple[int, float]]],
    N: int,
    max_k: int = 60,
    min_k: int = 2,
) -> np.ndarray:
    try:
        import scipy.sparse as sp
        from scipy.sparse.linalg import eigsh
        from utils.track_graph import _kmeans

        rows_indices = []
        cols_indices = []
        data = []
        for i, cands in enumerate(candidates):
            for j, w in cands:
                rows_indices.append(i)
                cols_indices.append(j)
                data.append(w)
        W = sp.csr_matrix((data, (rows_indices, cols_indices)), shape=(N, N), dtype=np.float64)
        W = (W + W.T) / 2.0
        W.setdiag(0.0)
        W.eliminate_zeros()

        d = np.array(W.sum(axis=1)).flatten()
        d_inv_sqrt = np.zeros(N, dtype=np.float64)
        mask = d > 1e-12
        d_inv_sqrt[mask] = 1.0 / np.sqrt(d[mask])
        D_inv_sqrt = sp.diags(d_inv_sqrt)
        P = D_inv_sqrt @ W @ D_inv_sqrt

        diag_vals = np.zeros(N, dtype=np.float64)
        diag_vals[d <= 1e-12] = 1.0
        P = P + sp.diags(diag_vals)

        n_eig = min(max_k + 1, N - 1)
        eigenvalues_P, eigenvectors = eigsh(P, k=n_eig, which='LA')

        sort_idx = np.argsort(eigenvalues_P)[::-1]
        eigenvalues_P = eigenvalues_P[sort_idx]
        eigenvectors = eigenvectors[:, sort_idx]

        eigenvalues = 1.0 - eigenvalues_P

    except Exception:
        W = np.zeros((N, N), dtype=np.float64)
        for i, cands in enumerate(candidates):
            for j, w in cands:
                W[i, j] = max(W[i, j], w)
        W = (W + W.T) / 2.0
        np.fill_diagonal(W, 0.0)

        d = W.sum(axis=1)
        d_inv_sqrt = np.zeros(N, dtype=np.float64)
        mask = d > 1e-12
        d_inv_sqrt[mask] = 1.0 / np.sqrt(d[mask])
        D_inv_sqrt = np.diag(d_inv_sqrt)

        P = D_inv_sqrt @ W @ D_inv_sqrt
        for i in range(N):
            if d[i] <= 1e-12:
                P[i, i] = 1.0
        L_sym = np.eye(N, dtype=np.float64) - P

        n_eig = min(max_k + 1, N)
        eigenvalues, eigenvectors = np.linalg.eigh(L_sym)
        eigenvalues = eigenvalues[:n_eig]
        eigenvectors = eigenvectors[:, :n_eig]

    gaps = np.diff(eigenvalues[1:])
    if len(gaps) == 0:
        return np.zeros(N, dtype=np.int32)
    
    adaptive_min_k = max(min_k, min(30, int(np.sqrt(N) / 1.8)))
    start_idx = max(0, adaptive_min_k - 2)
    if start_idx < len(gaps):
        best_gap_idx = start_idx + int(np.argmax(gaps[start_idx:]))
    else:
        best_gap_idx = len(gaps) - 1

    k = best_gap_idx + 2
    k = max(min_k, min(k, max_k, N))

    U = eigenvectors[:, :k].copy()
    row_norms = np.linalg.norm(U, axis=1, keepdims=True)
    row_norms = np.where(row_norms < 1e-10, 1.0, row_norms)
    U = U / row_norms

    try:
        from sklearn.cluster import KMeans
        kmeans = KMeans(n_clusters=k, init='k-means++', n_init=5, max_iter=50, random_state=42)
        labels = kmeans.fit_predict(U)
    except ImportError:
        labels = _kmeans(U, k, max_iter=50, n_restarts=5)
    return labels


def get_redundant_features(rows, projection, eigenvalues, threshold=0.70) -> set[str]:
    if len(rows) < 50 or projection is None or eigenvalues is None:
        return set()
        
    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    N = len(rows)
    D = len(raw_features)
    
    X = np.zeros((N, D), dtype=np.float32)
    for idx, r in enumerate(rows):
        feat_vec = []
        for f in raw_features:
            if f == "key_mode":
                ki = r.get("key_index", 0) or 0
                val = 1.0 if ki < 12 else 0.0
            else:
                val = float(r.get(f, 0) or 0)
            feat_vec.append(val)
        X[idx, :] = feat_vec
        
    stds = np.std(X, axis=0)
    stds[stds == 0] = 1.0
    X_norm = (X - np.mean(X, axis=0)) / stds
    corr_matrix = np.dot(X_norm.T, X_norm) / (N - 1)
    
    overall_weights = []
    for feat_idx, feat in enumerate(raw_features):
        weight = float(
            (projection[feat_idx, 0] ** 2) * eigenvalues[0] +
            (projection[feat_idx, 1] ** 2) * eigenvalues[1] +
            (projection[feat_idx, 2] ** 2) * eigenvalues[2]
        )
        overall_weights.append((feat, feat_idx, weight))
        
    sorted_features = sorted(overall_weights, key=lambda x: x[2], reverse=True)
    
    redundant = set()
    for i in range(len(sorted_features)):
        feat_i, idx_i, weight_i = sorted_features[i]
        for j in range(i):
            feat_j, idx_j, weight_j = sorted_features[j]
            if feat_j in redundant:
                continue
            r = abs(corr_matrix[idx_i, idx_j])
            if r >= threshold:
                redundant.add(feat_i)
                break
                
    return redundant


# Deterministic Spectral Clustering (computes exactly k eigenvectors)
def spectral_cluster_deterministic(candidates, N, k):
    import scipy.sparse as sp
    from scipy.sparse.linalg import eigsh
    from utils.track_graph import _kmeans
    
    # Build W
    rows_indices = []
    cols_indices = []
    data = []
    for i, cands in enumerate(candidates):
        for j, w in cands:
            rows_indices.append(i)
            cols_indices.append(j)
            data.append(w)
    W = sp.csr_matrix((data, (rows_indices, cols_indices)), shape=(N, N), dtype=np.float64)
    W = (W + W.T) / 2.0
    W.setdiag(0.0)
    W.eliminate_zeros()

    # Degree matrix and normalised affinity P
    d = np.array(W.sum(axis=1)).flatten()
    d_inv_sqrt = np.zeros(N, dtype=np.float64)
    mask = d > 1e-12
    d_inv_sqrt[mask] = 1.0 / np.sqrt(d[mask])
    D_inv_sqrt = sp.diags(d_inv_sqrt)
    P = D_inv_sqrt @ W @ D_inv_sqrt

    diag_vals = np.zeros(N, dtype=np.float64)
    diag_vals[d <= 1e-12] = 1.0
    P = P + sp.diags(diag_vals)

    # Eigendecomposition for exactly k eigenvectors
    eigenvalues_P, eigenvectors = eigsh(P, k=k, which='LA')
    
    sort_idx = np.argsort(eigenvalues_P)[::-1]
    eigenvectors = eigenvectors[:, sort_idx]

    # NJW normalization
    U = eigenvectors[:, :k].copy()
    row_norms = np.linalg.norm(U, axis=1, keepdims=True)
    row_norms = np.where(row_norms < 1e-10, 1.0, row_norms)
    U = U / row_norms

    # KMeans
    try:
        from sklearn.cluster import KMeans
        kmeans = KMeans(n_clusters=k, init='k-means++', n_init=5, max_iter=50, random_state=42)
        labels = kmeans.fit_predict(U)
    except ImportError:
        labels = _kmeans(U, k, max_iter=50, n_restarts=5)
    return labels


# Direct K-Means on Zn
def direct_kmeans(Zn, k):
    from utils.track_graph import _kmeans
    try:
        from sklearn.cluster import KMeans
        kmeans = KMeans(n_clusters=k, init='k-means++', n_init=5, max_iter=50, random_state=42)
        labels = kmeans.fit_predict(Zn)
    except ImportError:
        labels = _kmeans(Zn, k, max_iter=50, n_restarts=5)
    return labels


def prepare_vectors(rows, z_score: bool, use_redundant_pruning: bool, scalar_weight: float):
    redundant = set()
    if use_redundant_pruning:
        try:
            from utils.pca_engine import calculate_pca_projection
            means_pca, stds_pca, V_keep_pca, eigenvalues_pca, _ = calculate_pca_projection(rows)
            redundant = get_redundant_features(rows, V_keep_pca, eigenvalues_pca)
        except Exception as e:
            redundant = set()

    paths = []
    vectors = []
    for r in rows:
        v = tg.unpack_timbre(r.get("timbre"))
        if v is None or v.shape[0] != tg.EMBED_DIMS:
            continue
        bpm_raw = float(r.get("bpm", 0) or 0)
        log_bpm = float(np.log2(max(bpm_raw, 1.0)))
        ki = r.get("key_index", 0) or 0
        cam = key_index_to_camelot(ki)
        if cam is None:
            cos_h, sin_h, key_mode = 0.0, 0.0, 0.0
        else:
            hour, ring = cam
            theta = 2.0 * np.pi * (hour - 1) / 12.0
            cos_h = float(np.cos(theta))
            sin_h = float(np.sin(theta))
            key_mode = 1.0 if ring == "B" else 0.0

        active_scalars = []
        if "bpm" not in redundant:
            active_scalars.append(log_bpm)
        if "brightness" not in redundant:
            active_scalars.append(r.get("brightness", 0) or 0)
        if "energy" not in redundant:
            active_scalars.append(r.get("energy", 0) or 0)
        if "rolloff" not in redundant:
            active_scalars.append(r.get("rolloff", 0) or 0)
        if "beat_strength" not in redundant:
            active_scalars.append(r.get("beat_strength", 0) or 0)
        if "spectral_flatness" not in redundant:
            active_scalars.append(r.get("spectral_flatness", 0) or 0)
        if "spectral_contrast" not in redundant:
            active_scalars.append(r.get("spectral_contrast", 0) or 0)

        active_scalars.append(cos_h)
        active_scalars.append(sin_h)
        if "key_mode" not in redundant:
            active_scalars.append(key_mode)

        scalars = np.array(active_scalars, dtype=np.float32)
        paths.append(r["path"])
        vectors.append(np.concatenate([v.astype(np.float32), scalars]))

    X = np.stack(vectors, axis=0)
    mu = X.mean(axis=0, keepdims=True)
    if z_score:
        sd = X.std(axis=0, keepdims=True)
        sd = np.where(sd < 1e-8, 1.0, sd)
        Z = (X - mu) / sd
    else:
        Z = X - mu

    # Apply scalar boost AFTER scaling
    if scalar_weight != 1.0:
        Z[:, tg.EMBED_DIMS:] *= scalar_weight

    # PCA reduction
    N = Z.shape[0]
    _U, _S, _Vt = np.linalg.svd(Z, full_matrices=False)
    eigenvalues = (_S ** 2) / float(N - 1)
    kaiser_k = int((eigenvalues > 1.0).sum())
    kaiser_k = max(3, min(kaiser_k, _Vt.shape[0]))
    Zr = Z @ _Vt[:kaiser_k].T
    norms_r = np.linalg.norm(Zr, axis=1, keepdims=True)
    norms_r = np.where(norms_r < 1e-8, 1.0, norms_r)
    Zn = (Zr / norms_r).astype(np.float32)

    return paths, Zn, redundant


def build_pruned_candidates(Zn, N):
    candidates = [[] for _ in range(N)]
    chunk = 256
    for i in range(0, N, chunk):
        block = Zn[i:i + chunk]
        sims = block @ Zn.T
        for j, row_idx in enumerate(range(i, i + block.shape[0])):
            sims[j, row_idx] = -np.inf
        topk_unsorted = np.argpartition(-sims, 20, axis=1)[:, :20]
        for j, row_idx in enumerate(range(i, i + block.shape[0])):
            idx = topk_unsorted[j]
            order = np.argsort(-sims[j, idx])
            ordered = idx[order]
            candidates[row_idx] = [(int(nb), float(sims[j, nb])) for nb in ordered]

    sigmas = np.ones(N, dtype=np.float32)
    for i in range(N):
        pivot = candidates[i][min(6, len(candidates[i]) - 1)][1]
        sigmas[i] = float(np.sqrt(max(0.0, 2.0 * (1.0 - pivot))))
    sigmas = np.maximum(sigmas, 1e-3)

    rescaled = [[] for _ in range(N)]
    for i, cands in enumerate(candidates):
        sigma_i = float(sigmas[i])
        for nb, cos_sim in cands:
            d2 = max(0.0, 2.0 * (1.0 - cos_sim))
            affinity = float(np.exp(-d2 / (sigma_i * float(sigmas[nb]))))
            rescaled[i].append((nb, affinity))
    candidates = rescaled

    neighbour_set = [{nb for nb, _ in cands} for cands in candidates]
    pruned_candidates = [[] for _ in range(N)]
    for i, cands in enumerate(candidates):
        for j, w in cands:
            if i in neighbour_set[j]:
                pruned_candidates[i].append((j, w))
    return pruned_candidates


def evaluate_clustering(labels, rows, paths):
    # Group tracks by cluster
    clusters = {}
    for idx, path in enumerate(paths):
        cid = labels[idx]
        track = next(r for r in rows if r["path"] == path)
        clusters.setdefault(cid, []).append(track)

    num_clusters = len(clusters)
    sizes = [len(tracks) for tracks in clusters.values()]
    min_size = min(sizes) if sizes else 0
    max_size = max(sizes) if sizes else 0
    mean_size = np.mean(sizes) if sizes else 0.0
    median_size = np.median(sizes) if sizes else 0.0

    bpm_stds = []
    cam_dists = []
    mode_consistencies = []

    for cid, tracks in clusters.items():
        if len(tracks) < 2:
            continue
            
        bpms = [float(t.get("bpm", 0.0) or 0.0) for t in tracks]
        bpm_stds.append(np.std(bpms))

        dists = []
        for i in range(len(tracks)):
            for j in range(i + 1, len(tracks)):
                dists.append(camelot_distance(tracks[i].get("key_index", 0) or 0, tracks[j].get("key_index", 0) or 0))
        if dists:
            cam_dists.append(np.mean(dists))

        modes = []
        for t in tracks:
            ki = t.get("key_index", 0) or 0
            cam = key_index_to_camelot(ki)
            mode = 1 if cam and cam[1] == "B" else 0
            modes.append(mode)
        frac_major = np.mean(modes)
        mode_consistencies.append(max(frac_major, 1.0 - frac_major))

    avg_bpm_std = np.mean(bpm_stds) if bpm_stds else 0.0
    avg_cam_dist = np.mean(cam_dists) if cam_dists else 0.0
    avg_mode_consistency = np.mean(mode_consistencies) if mode_consistencies else 0.0

    idx_skepta = next((i for i, p in enumerate(paths) if "Glow in the Dark" in next(r.get("title", "") for r in rows if r["path"] == p)), None)
    idx_maz = next((i for i, p in enumerate(paths) if "Ores Mikres" in next(r.get("title", "") for r in rows if r["path"] == p)), None)
    
    skepta_cluster = labels[idx_skepta] if idx_skepta is not None else -1
    maz_cluster = labels[idx_maz] if idx_maz is not None else -1
    separated = (skepta_cluster != maz_cluster) if (skepta_cluster != -1 and maz_cluster != -1) else True

    return {
        "num_clusters": num_clusters,
        "min_size": min_size,
        "max_size": max_size,
        "mean_size": mean_size,
        "median_size": median_size,
        "avg_bpm_std": avg_bpm_std,
        "avg_cam_dist": avg_cam_dist,
        "avg_mode_consistency": avg_mode_consistency,
        "separated": separated,
        "skepta_cluster": skepta_cluster,
        "maz_cluster": maz_cluster,
        "clusters": clusters
    }


async def main() -> None:
    bundle = _resolve_bundle()
    print(f"Using bundle: {bundle}")
    extract_dir = _extract(bundle)
    work_db = os.path.join(extract_dir, "work.db")
    shutil.copy2(os.path.join(extract_dir, "library.db"), work_db)

    db = DatabaseManager(work_db)
    
    print("\n[1] Loading music library data...")
    rows = await db.get_tracks_with_features(tg.FEATURES_VERSION)
    N = len(rows)
    print(f"Loaded {N} tracks with features.")

    # Define configurations to run and compare
    CONFIGS = [
        {
            "id": 1,
            "name": "Original SC (Eigengap, Baseline Features)",
            "method": "original_sc",
            "k": None,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 2,
            "name": "Deterministic SC (k=30, Baseline Features)",
            "method": "deterministic_sc",
            "k": 30,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 3,
            "name": "Deterministic SC (k=45, Baseline Features)",
            "method": "deterministic_sc",
            "k": 45,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 4,
            "name": "Deterministic SC (k=60, Baseline Features)",
            "method": "deterministic_sc",
            "k": 60,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 5,
            "name": "Direct K-Means (k=30, Baseline Features)",
            "method": "direct_kmeans",
            "k": 30,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 6,
            "name": "Direct K-Means (k=45, Baseline Features)",
            "method": "direct_kmeans",
            "k": 45,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 7,
            "name": "Direct K-Means (k=60, Baseline Features)",
            "method": "direct_kmeans",
            "k": 60,
            "z_score": True,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        },
        {
            "id": 8,
            "name": "Direct K-Means (k=45, All Scalars, No Pruning)",
            "method": "direct_kmeans",
            "k": 45,
            "z_score": True,
            "use_redundant_pruning": False,
            "scalar_weight": 1.0
        },
        {
            "id": 9,
            "name": "Direct K-Means (k=45, Scalar Boosted 3x)",
            "method": "direct_kmeans",
            "k": 45,
            "z_score": True,
            "use_redundant_pruning": False,
            "scalar_weight": 3.0
        },
        {
            "id": 10,
            "name": "Direct K-Means (k=45, Scalar Boosted 1.5x)",
            "method": "direct_kmeans",
            "k": 45,
            "z_score": True,
            "use_redundant_pruning": False,
            "scalar_weight": 1.5
        },
        {
            "id": 11,
            "name": "Direct K-Means (k=45, Scalar Boosted 2.0x)",
            "method": "direct_kmeans",
            "k": 45,
            "z_score": True,
            "use_redundant_pruning": False,
            "scalar_weight": 2.0
        },
        {
            "id": 12,
            "name": "Direct K-Means (k=45, No Z-Score)",
            "method": "direct_kmeans",
            "k": 45,
            "z_score": False,
            "use_redundant_pruning": True,
            "scalar_weight": 1.0
        }
    ]

    results = []

    print("\n[2] Running cluster evaluations...")
    for cfg in CONFIGS:
        print(f"  * Running Config {cfg['id']}: {cfg['name']}...")
        
        # Prepare feature coordinates Zn
        paths, Zn, redundant = prepare_vectors(
            rows, 
            z_score=cfg["z_score"], 
            use_redundant_pruning=cfg["use_redundant_pruning"], 
            scalar_weight=cfg["scalar_weight"]
        )
        
        # Run clustering
        t0 = time.perf_counter()
        if cfg["method"] == "original_sc":
            pruned_candidates = build_pruned_candidates(Zn, len(paths))
            labels = _spectral_cluster(pruned_candidates, len(paths))
            k_run = len(np.unique(labels))
        elif cfg["method"] == "deterministic_sc":
            pruned_candidates = build_pruned_candidates(Zn, len(paths))
            labels = spectral_cluster_deterministic(pruned_candidates, len(paths), cfg["k"])
            k_run = cfg["k"]
        else: # direct_kmeans
            labels = direct_kmeans(Zn, cfg["k"])
            k_run = cfg["k"]
        elapsed = time.perf_counter() - t0
        
        # Evaluate quality
        metrics = evaluate_clustering(labels, rows, paths)
        metrics["time"] = elapsed
        metrics["cfg"] = cfg
        metrics["labels"] = labels
        metrics["paths"] = paths
        metrics["redundant"] = redundant
        results.append(metrics)
        
        print(f"    Completed in {elapsed:.4f}s. Obtained k={metrics['num_clusters']}. "
              f"Separated Skepta & Mazonakis? {metrics['separated']}")

    # 3. Print a beautiful summary table
    print("\n" + "="*90)
    print("CLUSTERING CONFIGURATIONS SUMMARY TABLE:")
    print("="*90)
    print(f"{'ID':<3} | {'Configuration Name':<45} | {'k':<3} | {'Time (s)':<8} | {'BPM Std':<7} | {'CamDist':<7} | {'ModeCons':<8} | {'Sep?'}")
    print("-" * 90)
    for r in results:
        cfg = r["cfg"]
        sep_str = "YES" if r["separated"] else "NO"
        print(f"{cfg['id']:<3} | {cfg['name'][:45]:<45} | {r['num_clusters']:<3} | {r['time']:<8.4f} | {r['avg_bpm_std']:<7.2f} | {r['avg_cam_dist']:<7.2f} | {r['avg_mode_consistency']:<8.2%} | {sep_str}")
    print("="*90)

    # 4. Write comparison report to tests/clustering_comparison_report.txt
    report_path = "tests/clustering_comparison_report.txt"
    print(f"\n[3] Writing detailed cluster listings for all configurations to {report_path}...")
    with open(report_path, "w", encoding="utf-8") as f_out:
        f_out.write("COMPREHENSIVE CLUSTERING COMPARISON REPORT\n")
        f_out.write("=" * 90 + "\n\n")
        
        # Table first
        f_out.write("SUMMARY OF CONFIGURATIONS:\n")
        f_out.write("-" * 90 + "\n")
        f_out.write(f"{'ID':<3} | {'Configuration Name':<45} | {'k':<3} | {'Time (s)':<8} | {'BPM Std':<7} | {'CamDist':<7} | {'ModeCons':<8} | {'Sep?'}\n")
        f_out.write("-" * 90 + "\n")
        for r in results:
            cfg = r["cfg"]
            sep_str = "YES" if r["separated"] else "NO"
            f_out.write(f"{cfg['id']:<3} | {cfg['name'][:45]:<45} | {r['num_clusters']:<3} | {r['time']:<8.4f} | {r['avg_bpm_std']:<7.2f} | {r['avg_cam_dist']:<7.2f} | {r['avg_mode_consistency']:<8.2%} | {sep_str}\n")
        f_out.write("=" * 90 + "\n\n\n")
        
        # Detailed track listings for each config
        for r in results:
            cfg = r["cfg"]
            f_out.write(f"=== CONFIG {cfg['id']}: {cfg['name']} ===\n")
            f_out.write(f"Parameters: Z-Score={cfg['z_score']}, Prune Redundant={cfg['use_redundant_pruning']}, Scalar Weight={cfg['scalar_weight']}\n")
            f_out.write(f"Metrics: Time={r['time']:.4f}s, k={r['num_clusters']}, BPM Std Dev={r['avg_bpm_std']:.2f}, Camelot Dist={r['avg_cam_dist']:.2f}, Key Mode Consistency={r['avg_mode_consistency']:.2%}\n")
            f_out.write(f"Pruned Redundant Features: {sorted(list(r['redundant'])) if r['redundant'] else 'None'}\n")
            f_out.write("=" * 90 + "\n\n")
            
            clusters = r["clusters"]
            for cid, tracks in sorted(clusters.items()):
                # Profile stats for the cluster
                bpms = [float(t.get("bpm", 0.0) or 0.0) for t in tracks]
                cluster_bpm_mean = np.mean(bpms) if bpms else 0.0
                cluster_bpm_std = np.std(bpms) if bpms else 0.0
                
                modes = []
                for t in tracks:
                    ki = t.get("key_index", 0) or 0
                    cam = key_index_to_camelot(ki)
                    mode = 1 if cam and cam[1] == "B" else 0
                    modes.append(mode)
                major_ratio = np.mean(modes) if modes else 0.0
                
                f_out.write(f"Cluster {cid} (size = {len(tracks)} tracks, BPM: {cluster_bpm_mean:.1f} ±{cluster_bpm_std:.1f}, Major: {major_ratio:.1%})\n")
                f_out.write("-" * 60 + "\n")
                sorted_tracks = sorted(tracks, key=lambda x: (x.get("artist") or "", x.get("title") or ""))
                for t in sorted_tracks:
                    ki = t.get("key_index", 0) or 0
                    bpm = float(t.get("bpm", 0) or 0)
                    f_out.write(f"  * {t.get('artist')} - {t.get('title')} (BPM: {bpm:.0f}, Key: {_camelot_str(ki)})\n")
                f_out.write("\n")
            f_out.write("\n" + "=" * 90 + "\n\n")
            
    print("Done writing report.")

    if db._conn is not None:
        await db._conn.close()


if __name__ == "__main__":
    asyncio.run(main())
