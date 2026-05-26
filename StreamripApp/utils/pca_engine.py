import numpy as np
import logging

logger = logging.getLogger(__name__)

# Participative raw features in PCA calculation
_RAW_FEATURES = [
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast", "key_mode",
]

def extract_feature_vector(row: dict) -> list[float]:
    """Extracts and converts raw DB features to a consistent float list.
    Specifically handles the projection of discrete key_index into binary key_mode."""
    feat = []
    for f in _RAW_FEATURES:
        if f == "key_mode":
            ki = row.get("key_index", 0) or 0
            val = 1.0 if ki < 12 else 0.0
        else:
            val = float(row.get(f, 0) or 0)
        feat.append(val)
    return feat

def calculate_pca_projection(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """
    Computes PCA using a fully automated unsupervised double-pass correlation filter:
    1. First-pass PCA SVD to find initial variance loadings and eigenvalues.
    2. Covariance (Pearson correlation) analysis of the raw features.
    3. Prunes features correlating at |r| >= 0.85 along descending explained variance order.
    4. Second-pass PCA SVD on the active non-redundant feature subset to obtain the optimized orthogonal space.
    5. Assembles a final 8x3 projection matrix V_keep with zero-padding on redundant rows.
    
    Returns:
        means (np.ndarray): 8-element mean vector (mu)
        stds (np.ndarray): 8-element standard deviation vector (sigma)
        V_keep (np.ndarray): 8x3 projection matrix (eigenvectors)
        eigenvalues (np.ndarray): 8 eigenvalues sorted descending (from active pass)
        kaiser_k (int): Count of eigenvalues >= 1.0
    """
    N = len(rows)
    D = len(_RAW_FEATURES)
    
    # Defaults in case of tiny library sizes
    default_means = np.zeros(D, dtype=np.float32)
    default_stds = np.ones(D, dtype=np.float32)
    default_V_keep = np.eye(D, 3, dtype=np.float32)
    default_eigenvalues = np.zeros(D, dtype=np.float32)
    
    if N < 2:
        logger.warning("Library has fewer than 2 tracks. Using fallback identity projection.")
        return default_means, default_stds, default_V_keep, default_eigenvalues, 3
        
    # Build data matrix X
    X = np.zeros((N, D), dtype=np.float32)
    for idx, r in enumerate(rows):
        X[idx, :] = extract_feature_vector(r)
        
    # 1. Compute scaling factors (mean and std dev)
    means = np.mean(X, axis=0)
    stds = np.std(X, axis=0)
    stds[stds == 0] = 1.0
    
    # Z-score standard scaling
    X_scaled = (X - means) / stds
    
    # === STEP 1: FIRST-PASS PCA (Find baseline loadings & eigenvalues) ===
    try:
        U, S, Vt = np.linalg.svd(X_scaled, full_matrices=False)
        eigenvalues = (S ** 2) / (N - 1)
        loadings = Vt.T
    except Exception as e:
        logger.error(f"First-pass SVD computation failed: {e}. Falling back.")
        return default_means, default_stds, default_V_keep, default_eigenvalues, 3
        
    # === STEP 2: COVARIANCE (PEARSON CORRELATION) ANALYSIS & PRUNING ===
    corr_matrix = np.corrcoef(X_scaled.T)
    
    # Calculate explained variance weights to sort features
    # Use however many PCs the SVD actually returned (rank can be < 3 for tiny data)
    n_pcs = min(3, len(eigenvalues))
    overall_weights = []
    for feat_idx, feat in enumerate(_RAW_FEATURES):
        weight = float(sum(
            (loadings[feat_idx, pc] ** 2) * eigenvalues[pc]
            for pc in range(n_pcs)
        ))
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
            if r >= 0.85:
                redundant.add(feat_i)
                logger.warning(
                    f"PCA Engine: Dynamic Cleaving redundant feature '{feat_i}' (correlated with '{feat_j}', r={r:.2f})"
                )
                break
                
    active_indices = [idx for idx in range(D) if _RAW_FEATURES[idx] not in redundant]
    
    # Fallback to standard full PCA if library is very small (< 50 tracks) or pruning leaves too few features
    if len(active_indices) < 2 or N < 50:
        M = Vt.shape[0]
        Vt_padded = np.vstack([Vt, np.zeros((3 - M, D), dtype=np.float32)]) if M < 3 else Vt[:3, :]
        V_keep = Vt_padded.T
        V_keep = np.ascontiguousarray(V_keep, dtype=np.float32)
        
        if len(eigenvalues) < D:
            eigenvalues_padded = np.zeros(D, dtype=np.float32)
            eigenvalues_padded[:len(eigenvalues)] = eigenvalues
            eigenvalues = eigenvalues_padded
            
        return means, stds, V_keep, eigenvalues, int(np.sum(eigenvalues >= 1.0))
        
    # === STEP 3: SECOND-PASS PCA SVD (ACTIVE FEATURES ONLY) ===
    X_pruned = X[:, active_indices]
    means_p = np.mean(X_pruned, axis=0)
    stds_p = np.std(X_pruned, axis=0)
    stds_p[stds_p == 0] = 1.0
    X_scaled_p = (X_pruned - means_p) / stds_p
    
    try:
        U_p, S_p, Vt_p = np.linalg.svd(X_scaled_p, full_matrices=False)
        eigenvalues_p = (S_p ** 2) / (N - 1)
        loadings_p = Vt_p.T
    except Exception as e:
        logger.error(f"Second-pass SVD computation failed: {e}. Falling back.")
        M = Vt.shape[0]
        Vt_padded = np.vstack([Vt, np.zeros((3 - M, D), dtype=np.float32)]) if M < 3 else Vt[:3, :]
        V_keep = Vt_padded.T
        return means, stds, V_keep, eigenvalues, int(np.sum(eigenvalues >= 1.0))
        
    # === STEP 4: ASSEMBLE ZERO-PADDED 8x3 PROJECTION MATRIX ===
    # For active features, we map their scaled coordinates to SVD loadings.
    # For redundant features, their corresponding loading rows are zeroed.
    V_keep = np.zeros((D, 3), dtype=np.float32)
    for active_seq_idx, active_raw_idx in enumerate(active_indices):
        # Vt_p shape is (len(active), len(active)), Vt_p[:3, :] contains top 3 PCs.
        # Transposed loaders loadings_p has PCs in columns. So loadings_p[:, :3] is top 3 PC eigenvectors.
        # Ensure we align and pad if SVD rank of active features is less than 3
        M_p = Vt_p.shape[0]
        if M_p < 3:
            padding = np.zeros((3 - M_p, len(active_indices)), dtype=np.float32)
            Vt_p_padded = np.vstack([Vt_p, padding])
        else:
            Vt_p_padded = Vt_p[:3, :]
        loadings_p_clean = Vt_p_padded.T
        V_keep[active_raw_idx, :] = loadings_p_clean[active_seq_idx, :]
        
    V_keep = np.ascontiguousarray(V_keep, dtype=np.float32)
    
    # Format second-pass eigenvalues to length D
    eigenvalues_out = np.zeros(D, dtype=np.float32)
    eigenvalues_out[:len(eigenvalues_p)] = eigenvalues_p
    
    kaiser_k = int(np.sum(eigenvalues_p >= 1.0))
    logger.info(f"Unsupervised double-pass PCA completed. Kaiser selected active components: {kaiser_k}")
    
    return means, stds, V_keep, eigenvalues_out, kaiser_k

def project_track(row: dict, means: np.ndarray, stds: np.ndarray, V_keep: np.ndarray) -> np.ndarray:
    """Projects a single raw track row into the 3D orthogonal PCA space."""
    x = np.array(extract_feature_vector(row), dtype=np.float32)
    # Standardize
    x_scaled = (x - means) / stds
    # Multiply by loadings to project
    z = np.dot(x_scaled, V_keep)
    return z


# ─── Visualization ────────────────────────────────────────────────────────────

_FRIENDLY_NAMES = {
    "bpm":               "Tempo (BPM)",
    "brightness":        "Brightness",
    "energy":            "Energy",
    "rolloff":           "Treble Rolloff",
    "beat_strength":     "Beat Strength",
    "spectral_flatness": "Spectral Flatness",
    "spectral_contrast": "Spectral Contrast",
    "key_mode":          "Key Mode",
}

_DARK_THEME = {
    "figure.facecolor": "#121212",
    "axes.facecolor":   "#1E1E1E",
    "text.color":       "#FFFFFF",
    "axes.labelcolor":  "#FFFFFF",
    "xtick.color":      "#B3B3B3",
    "ytick.color":      "#B3B3B3",
    "font.size":        10,
    "axes.titlesize":   14,
    "axes.labelsize":   11,
}


def plot_pca_report(rows: list[dict], output_dir: str) -> list[str]:
    """Generate a four-figure PCA mathematical truth report and save PNGs to *output_dir*.

    Produces two pairs of figures (heatmap + scatter) — one pair for the full
    8-feature space and one for the unsupervised-pruned space — exactly
    mirroring the logic in tools/pca_analysis.py so the on-device output is
    mathematically identical.

    Returns a list of absolute paths to the saved files (empty if
    matplotlib/seaborn are not available or too few tracks exist).
    """
    try:
        import matplotlib
        matplotlib.use("Agg")          # headless / no display required on Android
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError:
        logger.warning(
            "plot_pca_report: matplotlib/seaborn not available — skipping visualization."
        )
        return []

    N = len(rows)
    D = len(_RAW_FEATURES)

    if N < 2:
        logger.warning("plot_pca_report: fewer than 2 tracks; skipping.")
        return []

    import os
    os.makedirs(output_dir, exist_ok=True)

    # ── Build raw feature matrix ──────────────────────────────────────────────
    X = np.zeros((N, D), dtype=np.float32)
    for idx, r in enumerate(rows):
        X[idx, :] = extract_feature_vector(r)

    # ── First-pass PCA (full 8-feature) ──────────────────────────────────────
    mu = np.mean(X, axis=0)
    sd = np.std(X, axis=0)
    sd[sd == 0] = 1.0
    X_scaled = (X - mu) / sd

    try:
        _, S, Vt = np.linalg.svd(X_scaled, full_matrices=False)
    except Exception as e:
        logger.error("plot_pca_report: SVD failed: %s", e)
        return []

    eigenvalues = (S ** 2) / (N - 1)
    total_var   = float(np.sum(eigenvalues))
    ev_ratio    = eigenvalues / total_var if total_var > 0 else np.zeros(D)
    loadings    = Vt.T                             # (D, D) — columns are PCs

    # ── Apply theme ───────────────────────────────────────────────────────────
    sns.set_theme(style="darkgrid", palette="muted")
    plt.rcParams.update(_DARK_THEME)

    saved: list[str] = []

    # helper: diverging palette
    def _divpal():
        return sns.diverging_palette(220, 20, as_cmap=True)

    # ══════════════════════════════════════════════════════════════════════════
    #  FIGURE SET 1 — FULL 8-FEATURE SPACE
    # ══════════════════════════════════════════════════════════════════════════
    corr_full   = np.corrcoef(X_scaled.T)
    names_full  = [_FRIENDLY_NAMES[f] for f in _RAW_FEATURES]

    # 1a. Heatmap (full) ───────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 8))
    mask = np.triu(np.ones_like(corr_full, dtype=bool))
    sns.heatmap(
        corr_full,
        mask=mask,
        xticklabels=names_full,
        yticklabels=names_full,
        cmap=_divpal(),
        vmax=1.0, vmin=-1.0, center=0,
        square=True, linewidths=0.5,
        cbar_kws={"shrink": 0.7},
        annot=True, fmt=".2f",
        annot_kws={"size": 9, "weight": "bold"},
        ax=ax,
    )
    ax.set_title(
        "Acoustic Feature Correlation Heatmap (Full — 8 Features)",
        pad=20, color="white", weight="bold",
    )
    fig.tight_layout()
    path_hm_full = os.path.join(output_dir, "covariance_heatmap_full.png")
    fig.savefig(path_hm_full, dpi=200, facecolor="#121212")
    plt.close(fig)
    saved.append(path_hm_full)
    logger.info("plot_pca_report: saved %s", path_hm_full)

    # 1b. PCA scatter (full) ───────────────────────────────────────────────────
    X_proj  = np.dot(X_scaled, loadings)
    energy  = X[:, _RAW_FEATURES.index("energy")]

    fig, ax = plt.subplots(figsize=(10, 8))
    sc = ax.scatter(
        X_proj[:, 0], X_proj[:, 1],
        c=energy, cmap="viridis",
        alpha=0.65, edgecolors="none", s=30,
    )
    scale = 3.5
    for i, feat in enumerate(_RAW_FEATURES):
        ax.arrow(0, 0,
                 loadings[i, 0] * scale, loadings[i, 1] * scale,
                 color="#FF4081", alpha=0.9, width=0.03, head_width=0.15)
        ax.text(
            loadings[i, 0] * scale * 1.15,
            loadings[i, 1] * scale * 1.15,
            _FRIENDLY_NAMES[feat],
            color="#FF4081", ha="center", va="center",
            fontsize=9, weight="bold",
        )
    cbar = fig.colorbar(sc, ax=ax)
    cbar.set_label("Energy (Raw Metric)", color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(plt.getp(cbar.ax.axes, "yticklabels"), color="#B3B3B3")
    ax.axhline(0, color="#333333", linestyle="--", linewidth=0.8)
    ax.axvline(0, color="#333333", linestyle="--", linewidth=0.8)
    ax.set_xlabel(f"PC1 ({ev_ratio[0]*100:.2f}% variance)")
    ax.set_ylabel(f"PC2 ({ev_ratio[1]*100:.2f}% variance)")
    ax.set_title(
        "Acoustic Principal Component Space (Full — 8 Features)",
        pad=20, color="white", weight="bold",
    )
    fig.tight_layout()
    path_sc_full = os.path.join(output_dir, "pca_scatter_full.png")
    fig.savefig(path_sc_full, dpi=200, facecolor="#121212")
    plt.close(fig)
    saved.append(path_sc_full)
    logger.info("plot_pca_report: saved %s", path_sc_full)

    # ══════════════════════════════════════════════════════════════════════════
    #  UNSUPERVISED REDUNDANCY CLEAVING (mirrors pca_engine double-pass logic)
    # ══════════════════════════════════════════════════════════════════════════
    overall_weights = []
    for feat_idx, feat in enumerate(_RAW_FEATURES):
        w = float(
            (loadings[feat_idx, 0] ** 2) * eigenvalues[0] +
            (loadings[feat_idx, 1] ** 2) * eigenvalues[1] +
            (loadings[feat_idx, 2] ** 2) * eigenvalues[2]
        )
        overall_weights.append((feat, feat_idx, w))

    sorted_features = sorted(overall_weights, key=lambda x: x[2], reverse=True)

    redundant: set[str] = set()
    for i, (feat_i, idx_i, _) in enumerate(sorted_features):
        for feat_j, idx_j, _ in sorted_features[:i]:
            if feat_j in redundant:
                continue
            if abs(corr_full[idx_i, idx_j]) >= 0.85:
                redundant.add(feat_i)
                logger.info(
                    "plot_pca_report: cleaving redundant feature '%s' (corr with '%s')",
                    feat_i, feat_j,
                )
                break

    active_indices  = [idx for idx, f in enumerate(_RAW_FEATURES) if f not in redundant]
    active_features = [_RAW_FEATURES[idx] for idx in active_indices]

    if len(active_features) < 2 or N < 50:
        logger.info(
            "plot_pca_report: skipping pruned plots "
            "(active=%d, N=%d — threshold not met or nothing cleaved).",
            len(active_features), N,
        )
        return saved

    # ══════════════════════════════════════════════════════════════════════════
    #  FIGURE SET 2 — PRUNED (CLEAVED) FEATURE SPACE
    # ══════════════════════════════════════════════════════════════════════════
    X_pruned   = X[:, active_indices]
    mu_p       = np.mean(X_pruned, axis=0)
    sd_p       = np.std(X_pruned,  axis=0)
    sd_p[sd_p == 0] = 1.0
    X_scaled_p = (X_pruned - mu_p) / sd_p

    try:
        _, S_p, Vt_p = np.linalg.svd(X_scaled_p, full_matrices=False)
    except Exception as e:
        logger.error("plot_pca_report: pruned SVD failed: %s", e)
        return saved

    ev_p         = (S_p ** 2) / (N - 1)
    ev_ratio_p   = ev_p / float(np.sum(ev_p))
    loadings_p   = Vt_p.T
    X_proj_p     = np.dot(X_scaled_p, loadings_p)
    corr_pruned  = np.corrcoef(X_scaled_p.T)
    names_pruned = [_FRIENDLY_NAMES[f] for f in active_features]

    # 2a. Heatmap (pruned) ─────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 8))
    mask_p = np.triu(np.ones_like(corr_pruned, dtype=bool))
    sns.heatmap(
        corr_pruned,
        mask=mask_p,
        xticklabels=names_pruned,
        yticklabels=names_pruned,
        cmap=_divpal(),
        vmax=1.0, vmin=-1.0, center=0,
        square=True, linewidths=0.5,
        cbar_kws={"shrink": 0.7},
        annot=True, fmt=".2f",
        annot_kws={"size": 9, "weight": "bold"},
        ax=ax,
    )
    ax.set_title(
        f"Acoustic Feature Correlation Heatmap (Pruned — {len(active_features)} Features)",
        pad=20, color="white", weight="bold",
    )
    fig.tight_layout()
    path_hm_pruned = os.path.join(output_dir, "covariance_heatmap_pruned.png")
    fig.savefig(path_hm_pruned, dpi=200, facecolor="#121212")
    plt.close(fig)
    saved.append(path_hm_pruned)
    logger.info("plot_pca_report: saved %s", path_hm_pruned)

    # 2b. PCA scatter (pruned) ─────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 8))
    sc_p = ax.scatter(
        X_proj_p[:, 0], X_proj_p[:, 1],
        c=energy, cmap="viridis",
        alpha=0.65, edgecolors="none", s=30,
    )
    for i, feat in enumerate(active_features):
        ax.arrow(0, 0,
                 loadings_p[i, 0] * scale, loadings_p[i, 1] * scale,
                 color="#FF4081", alpha=0.9, width=0.03, head_width=0.15)
        ax.text(
            loadings_p[i, 0] * scale * 1.15,
            loadings_p[i, 1] * scale * 1.15,
            _FRIENDLY_NAMES[feat],
            color="#FF4081", ha="center", va="center",
            fontsize=9, weight="bold",
        )
    cbar_p = fig.colorbar(sc_p, ax=ax)
    cbar_p.set_label("Energy (Raw Metric)", color="white")
    cbar_p.ax.yaxis.set_tick_params(color="white")
    plt.setp(plt.getp(cbar_p.ax.axes, "yticklabels"), color="#B3B3B3")
    ax.axhline(0, color="#333333", linestyle="--", linewidth=0.8)
    ax.axvline(0, color="#333333", linestyle="--", linewidth=0.8)
    ax.set_xlabel(f"PC1 ({ev_ratio_p[0]*100:.2f}% variance)")
    ax.set_ylabel(f"PC2 ({ev_ratio_p[1]*100:.2f}% variance)")
    ax.set_title(
        f"Acoustic Principal Component Space (Pruned — {len(active_features)} Features)",
        pad=20, color="white", weight="bold",
    )
    fig.tight_layout()
    path_sc_pruned = os.path.join(output_dir, "pca_scatter_pruned.png")
    fig.savefig(path_sc_pruned, dpi=200, facecolor="#121212")
    plt.close(fig)
    saved.append(path_sc_pruned)
    logger.info("plot_pca_report: saved %s", path_sc_pruned)

    return saved
