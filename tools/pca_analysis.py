#!/usr/bin/env python3
"""
PCA analysis script for StreamripApp DSP features.
Author: Antigravity
Description: Extracts DSP features from the SQLite library database,
             performs Principal Component Analysis (PCA) using numpy SVD,
             and reports explained variance and feature loadings to see
             which acoustic attributes drive track differences.
"""

import os
import sqlite3
import numpy as np

# Feature columns participating in mood scoring
_MOOD_FEATURES = [
    "bpm", "brightness", "energy", "rolloff", "beat_strength",
    "spectral_flatness", "spectral_contrast", "key_mode",
]

def load_data(db_path):
    """Loads features from the SQLite database."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found at {db_path}")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Let's see if the play_counts table exists and what tracks have features
    try:
        cursor.execute("SELECT COUNT(*) FROM play_counts WHERE timbre IS NOT NULL")
        count = cursor.fetchone()[0]
        if count == 0:
            print(f"Warning: No tracks with populated features found in {db_path}")
            return [], []
    except Exception as e:
        print(f"Error checking table: {e}")
        return [], []
        
    sql = '''
        SELECT pc.track_path AS path,
               COALESCE(pc.bpm, 0)           AS bpm,
               COALESCE(pc.brightness, 0)    AS brightness,
               COALESCE(pc.energy, 0)        AS energy,
               COALESCE(pc.rolloff, 0)       AS rolloff,
               COALESCE(pc.beat_strength, 0) AS beat_strength,
               COALESCE(pc.spectral_flatness, 0) AS spectral_flatness,
               COALESCE(pc.spectral_contrast, 0) AS spectral_contrast,
               COALESCE(pc.key_index, 0)         AS key_index,
               t.title,
               ar.name  AS artist
        FROM play_counts pc
        LEFT JOIN tracks  t  ON t.path  = pc.track_path
        LEFT JOIN albums  al ON al.id   = t.album_id
        LEFT JOIN artists ar ON ar.id   = al.artist_id
        WHERE pc.timbre IS NOT NULL
    '''
    
    cursor.execute(sql)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    # Process features into numeric values
    data = []
    metadata = []
    for r in rows:
        feat = []
        for f in _MOOD_FEATURES:
            if f == "key_mode":
                ki = r.get("key_index", 0) or 0
                val = 1.0 if ki < 12 else 0.0
            else:
                val = float(r.get(f, 0) or 0)
            feat.append(val)
        data.append(feat)
        metadata.append({
            "path": r["path"],
            "title": r["title"] or "Unknown Title",
            "artist": r["artist"] or "Unknown Artist"
        })
        
    return np.array(data, dtype=np.float32), metadata

def compute_percentiles(X):
    """Computes column-wise percentile ranks ∈ [0, 1] as used in the app's distance formulas."""
    N, D = X.shape
    percentiles = np.zeros_like(X)
    if N <= 1:
        return percentiles
    for col in range(D):
        ranks = np.argsort(np.argsort(X[:, col]))
        percentiles[:, col] = ranks / float(N - 1)
    return percentiles

def run_pca(X, standardize=True):
    """Performs PCA via SVD. Returns (explained_variance, loadings, X_projected)."""
    N, D = X.shape
    if N <= 1:
        return np.zeros(D), np.zeros((D, D)), X
        
    # Center the data
    X_mean = np.mean(X, axis=0)
    X_centered = X - X_mean
    
    # Scale (Z-score)
    if standardize:
        X_std = np.std(X, axis=0)
        # Avoid division by zero for features with no variance
        X_std[X_std == 0] = 1.0
        X_scaled = X_centered / X_std
    else:
        X_scaled = X_centered
        
    # SVD on scaled data
    # X_scaled = U * S * Vt
    U, S, Vt = np.linalg.svd(X_scaled, full_matrices=False)
    
    # Eigenvalues / Variance
    eigenvalues = (S ** 2) / (N - 1)
    total_var = np.sum(eigenvalues)
    explained_variance_ratio = eigenvalues / total_var if total_var > 0 else np.zeros(D)
    
    # Loadings (eigenvectors)
    # Vt has shape (D, D), rows are PCs. Transpose so columns are PCs.
    loadings = Vt.T
    
    # Project data to PC space
    X_projected = np.dot(X_scaled, loadings)
    
    return explained_variance_ratio, loadings, X_projected

def print_markdown_report(db_name, db_path, X_raw, X_pct, metadata):
    N, D = X_raw.shape
    print(f"\n# PCA Statistical Report for Database: {db_name}")
    print(f"- **Path**: `{db_path}`")
    print(f"- **Tracks analyzed**: {N}")
    print(f"- **Features**: " + ", ".join([f"`{f}`" for f in _MOOD_FEATURES]))
    
    # 1. Feature Stats (Raw)
    print("\n## Feature Descriptive Statistics (Raw Values)")
    print("| Feature | Mean | Std Dev | Min | Max |")
    print("|---|---|---|---|---|")
    for i, f in enumerate(_MOOD_FEATURES):
        col_data = X_raw[:, i]
        print(f"| `{f}` | {np.mean(col_data):.4f} | {np.std(col_data):.4f} | {np.min(col_data):.4f} | {np.max(col_data):.4f} |")
        
    # 2. PCA on Raw (Standardized) features
    ev_raw, loadings_raw, proj_raw = run_pca(X_raw, standardize=True)
    
    print("\n## 1. PCA on Standardized Raw Features")
    print("This reveals the physical acoustic dimensions of variance in your catalog.")
    
    print("\n### Explained Variance Ratio")
    print("| Component | Eigenvalue | Explained Variance | Cumulative Variance |")
    print("|---|---|---|---|")
    cum_var = 0.0
    # Recompute eigenvalues for printing
    X_centered = X_raw - np.mean(X_raw, axis=0)
    X_std = np.std(X_raw, axis=0)
    X_std[X_std == 0] = 1.0
    X_scaled = X_centered / X_std
    _, S_raw, _ = np.linalg.svd(X_scaled, full_matrices=False)
    eigenvalues_raw = (S_raw ** 2) / (N - 1)
    
    for i in range(D):
        cum_var += ev_raw[i]
        print(f"| **PC{i+1}** | {eigenvalues_raw[i]:.4f} | {ev_raw[i]*100:.2f}% | {cum_var*100:.2f}% |")
        
    print("\n### Component Loadings (Eigenvectors)")
    print("Loadings represent the correlation of each feature with the principal component.")
    print("Values range from -1.0 to 1.0; larger absolute values mean stronger contribution.")
    
    header = "| Feature | " + " | ".join([f"**PC{i+1}**" for i in range(4)]) + " |"
    divider = "|---| " + " | ".join(["---" for _ in range(4)]) + " |"
    print(header)
    print(divider)
    for row_idx, f in enumerate(_MOOD_FEATURES):
        load_strs = []
        for col_idx in range(4):
            val = loadings_raw[row_idx, col_idx]
            # Bold high loading values for visual highlight
            if abs(val) >= 0.35:
                load_strs.append(f"**{val:+.4f}** *")
            else:
                load_strs.append(f"{val:+.4f}")
        print(f"| `{f}` | " + " | ".join(load_strs) + " |")

    # 3. PCA on Percentile Ranks
    ev_pct, loadings_pct, proj_pct = run_pca(X_pct, standardize=True)
    
    print("\n## 2. PCA on Percentile-Ranked Features (App Logic)")
    print("This shows the dimensions of variance that the app actually computes with.")
    
    print("\n### Explained Variance Ratio (Percentiles)")
    print("| Component | Explained Variance | Cumulative Variance |")
    print("|---|---|---|")
    cum_var_pct = 0.0
    for i in range(D):
        cum_var_pct += ev_pct[i]
        print(f"| **PC{i+1}** | {ev_pct[i]*100:.2f}% | {cum_var_pct*100:.2f}% |")
        
    print("\n### Component Loadings (Percentiles)")
    print(header)
    print(divider)
    for row_idx, f in enumerate(_MOOD_FEATURES):
        load_strs = []
        for col_idx in range(4):
            val = loadings_pct[row_idx, col_idx]
            if abs(val) >= 0.35:
                load_strs.append(f"**{val:+.4f}** *")
            else:
                load_strs.append(f"{val:+.4f}")
        print(f"| `{f}` | " + " | ".join(load_strs) + " |")
        
    # 4. Overall Key Takeaways and Interpretations
    print("\n## 3. Physical Interpretations")
    
    # Determine the top driving features of PC1 and PC2
    def get_pc_summary(loadings, features):
        summary = []
        for pc_idx in range(2):
            pc_loadings = loadings[:, pc_idx]
            sorted_indices = np.argsort(np.abs(pc_loadings))[::-1]
            pos_contribs = []
            neg_contribs = []
            for idx in sorted_indices:
                val = pc_loadings[idx]
                if abs(val) < 0.25:
                    continue
                feat_name = features[idx]
                if val > 0:
                    pos_contribs.append(f"`{feat_name}` ({val:+.2f})")
                else:
                    neg_contribs.append(f"`{feat_name}` ({val:+.2f})")
            summary.append((pos_contribs, neg_contribs))
        return summary
        
    raw_pc_summary = get_pc_summary(loadings_raw, _MOOD_FEATURES)
    pct_pc_summary = get_pc_summary(loadings_pct, _MOOD_FEATURES)
    
    print("Based on raw acoustic variance, we can map the mathematical axes of your catalog:")
    print(f"- **Principal Component 1 (PC1 - {ev_raw[0]*100:.1f}% variance)**:")
    print(f"  - **Positive poles** (high values): {', '.join(raw_pc_summary[0][0]) if raw_pc_summary[0][0] else 'None'}")
    print(f"  - **Negative poles** (low values): {', '.join(raw_pc_summary[0][1]) if raw_pc_summary[0][1] else 'None'}")
    print(f"- **Principal Component 2 (PC2 - {ev_raw[1]*100:.1f}% variance)**:")
    print(f"  - **Positive poles** (high values): {', '.join(raw_pc_summary[1][0]) if raw_pc_summary[1][0] else 'None'}")
    print(f"  - **Negative poles** (low values): {', '.join(raw_pc_summary[1][1]) if raw_pc_summary[1][1] else 'None'}")

    print("\n### Correlation Heatmap (Feature Covariance)")
    print("Strong correlations indicate redundant features that drive identical separation:")
    # Pearson Correlation Matrix
    X_centered_norm = X_raw - np.mean(X_raw, axis=0)
    std_devs = np.std(X_raw, axis=0)
    std_devs[std_devs == 0] = 1.0
    X_norm = X_centered_norm / std_devs
    corr_matrix = np.dot(X_norm.T, X_norm) / (N - 1)
    
    corr_header = "| Feature | " + " | ".join([f"`{f[:6]}`" for f in _MOOD_FEATURES]) + " |"
    corr_divider = "|---| " + " | ".join(["---" for _ in _MOOD_FEATURES]) + " |"
    print(corr_header)
    print(corr_divider)
    for i, f in enumerate(_MOOD_FEATURES):
        row_cells = []
        for j in range(len(_MOOD_FEATURES)):
            val = corr_matrix[i, j]
            if i == j:
                row_cells.append("`1.00`")
            elif abs(val) >= 0.5:
                row_cells.append(f"**{val:+.2f}** *")
            else:
                row_cells.append(f"{val:+.2f}")
        print(f"| `{f}` | " + " | ".join(row_cells) + " |")

def plot_visualizations(db_name, X_raw, loadings_raw, ev_raw, output_dir):
    """Generates covariance heatmap and PCA 2D scatter plots for both FULL and PRUNED feature sets."""
    try:
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError:
        print("\n[Notice] matplotlib and seaborn are required for visual plots. Install them using 'pip install matplotlib seaborn' to generate images.")
        return

    print("\nGenerating visual plots from actual library features...")
    N, D = X_raw.shape
    
    # Set premium dark-mode aesthetic
    sns.set_theme(style="darkgrid", palette="muted")
    plt.rcParams.update({
        'figure.facecolor': '#121212',
        'axes.facecolor': '#1E1E1E',
        'text.color': '#FFFFFF',
        'axes.labelcolor': '#FFFFFF',
        'xtick.color': '#B3B3B3',
        'ytick.color': '#B3B3B3',
        'font.size': 10,
        'axes.titlesize': 14,
        'axes.labelsize': 11
    })
    
    friendly_names_map = {
        "bpm": "Tempo (BPM)",
        "brightness": "Brightness",
        "energy": "Energy",
        "rolloff": "Treble Rolloff",
        "beat_strength": "Beat Strength",
        "spectral_flatness": "Spectral Flatness",
        "spectral_contrast": "Spectral Contrast",
        "key_mode": "Key Mode"
    }
    
    # ==================== FIGURE SET 1: FULL FEATURE SPACE (8 FEATURES) ====================
    print("Generating full feature space plots...")
    
    # 1.1 Covariance Heatmap (Full)
    corr_matrix_full = np.corrcoef(X_raw.T)
    friendly_names_full = [friendly_names_map[f] for f in _MOOD_FEATURES]
    
    plt.figure(figsize=(10, 8))
    mask_full = np.triu(np.ones_like(corr_matrix_full, dtype=bool))
    
    sns.heatmap(
        corr_matrix_full,
        mask=mask_full,
        xticklabels=friendly_names_full,
        yticklabels=friendly_names_full,
        cmap=sns.diverging_palette(220, 20, as_cmap=True),
        vmax=1.0, vmin=-1.0, center=0,
        square=True, linewidths=.5, cbar_kws={"shrink": .7},
        annot=True, fmt=".2f", annot_kws={"size": 9, "weight": "bold"}
    )
    
    plt.title("Acoustic Feature Correlation Heatmap (Full - 8 Features)", pad=20, color='white', weight='bold')
    plt.tight_layout()
    
    heatmap_full_path = os.path.join(output_dir, "covariance_heatmap_full.png")
    plt.savefig(heatmap_full_path, dpi=300, facecolor='#121212')
    plt.close()
    print(f"-> Saved: {heatmap_full_path}")
    
    # 1.2 PCA Scatter (Full)
    X_mean = np.mean(X_raw, axis=0)
    X_std = np.std(X_raw, axis=0)
    X_std[X_std == 0] = 1.0
    X_scaled = (X_raw - X_mean) / X_std
    X_projected = np.dot(X_scaled, loadings_raw)
    
    plt.figure(figsize=(10, 8))
    energy_values = X_raw[:, 2]  # energy is column index 2
    
    scatter_full = plt.scatter(
        X_projected[:, 0],
        X_projected[:, 1],
        c=energy_values,
        cmap="viridis",
        alpha=0.65,
        edgecolors='none',
        s=30
    )
    
    # Draw eigenvectors loadings
    scale_factor = 3.5
    for i, feature in enumerate(_MOOD_FEATURES):
        plt.arrow(
            0, 0, 
            loadings_raw[i, 0] * scale_factor, 
            loadings_raw[i, 1] * scale_factor, 
            color='#FF4081', 
            alpha=0.9, 
            width=0.03, 
            head_width=0.15
        )
        plt.text(
            loadings_raw[i, 0] * scale_factor * 1.15, 
            loadings_raw[i, 1] * scale_factor * 1.15, 
            friendly_names_map[feature], 
            color='#FF4081', 
            ha='center', 
            va='center',
            fontsize=9,
            weight='bold'
        )
        
    cbar_full = plt.colorbar(scatter_full)
    cbar_full.set_label("Energy (Raw Metric)", color='white')
    cbar_full.ax.yaxis.set_tick_params(color='white')
    plt.setp(plt.getp(cbar_full.ax.axes, 'yticklabels'), color='#B3B3B3')
    
    plt.axhline(0, color='#333333', linestyle='--', linewidth=0.8)
    plt.axvline(0, color='#333333', linestyle='--', linewidth=0.8)
    
    plt.xlabel(f"PC1 (variance: {ev_raw[0]*100:.2f}%)")
    plt.ylabel(f"PC2 (variance: {ev_raw[1]*100:.2f}%)")
    plt.title("Acoustic Principal Component Space (Full - 8 Features)", pad=20, color='white', weight='bold')
    plt.tight_layout()
    
    pca_full_path = os.path.join(output_dir, "pca_scatter_full.png")
    plt.savefig(pca_full_path, dpi=300, facecolor='#121212')
    plt.close()
    print(f"-> Saved: {pca_full_path}")
    
    # ==================== DYNAMIC REDUNDANCY DISCOVERY ====================
    # Calculate explained variance weights to sort features descending
    overall_weights = []
    for feat_idx, feat in enumerate(_MOOD_FEATURES):
        weight = float(
            (loadings_raw[feat_idx, 0] ** 2) * ev_raw[0] +
            (loadings_raw[feat_idx, 1] ** 2) * ev_raw[1] +
            (loadings_raw[feat_idx, 2] ** 2) * ev_raw[2]
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
            r = abs(corr_matrix_full[idx_i, idx_j])
            if r >= 0.85:
                redundant.add(feat_i)
                break
                
    active_indices = [idx for idx in range(D) if _MOOD_FEATURES[idx] not in redundant]
    active_features = [_MOOD_FEATURES[idx] for idx in active_indices]
    
    print("\n--- Unsupervised Cleaving Decision ---")
    print(f"Detected {len(redundant)} redundant features (threshold r>=0.85): " + ", ".join([f"'{f}'" for f in redundant]))
    print(f"Retained {len(active_features)} active features: " + ", ".join([f"'{f}'" for f in active_features]))
    
    if len(active_features) < 2:
        print("Not enough non-redundant features to plot pruned space.")
        return
        
    X_pruned = X_raw[:, active_indices]
    
    # ==================== FIGURE SET 2: PRUNED FEATURE SPACE ====================
    print("\nGenerating pruned feature space plots...")
    
    # 2.1 Covariance Heatmap (Pruned)
    corr_matrix_pruned = np.corrcoef(X_pruned.T)
    friendly_names_pruned = [friendly_names_map[f] for f in active_features]
    
    plt.figure(figsize=(10, 8))
    mask_pruned = np.triu(np.ones_like(corr_matrix_pruned, dtype=bool))
    
    sns.heatmap(
        corr_matrix_pruned,
        mask=mask_pruned,
        xticklabels=friendly_names_pruned,
        yticklabels=friendly_names_pruned,
        cmap=sns.diverging_palette(220, 20, as_cmap=True),
        vmax=1.0, vmin=-1.0, center=0,
        square=True, linewidths=.5, cbar_kws={"shrink": .7},
        annot=True, fmt=".2f", annot_kws={"size": 9, "weight": "bold"}
    )
    
    plt.title("Acoustic Feature Correlation Heatmap (Pruned - Cleaved)", pad=20, color='white', weight='bold')
    plt.tight_layout()
    
    heatmap_pruned_path = os.path.join(output_dir, "covariance_heatmap_pruned.png")
    plt.savefig(heatmap_pruned_path, dpi=300, facecolor='#121212')
    plt.close()
    print(f"-> Saved: {heatmap_pruned_path}")
    
    # 2.2 PCA Scatter (Pruned)
    # Perform standardized SVD on the pruned feature space
    X_mean_p = np.mean(X_pruned, axis=0)
    X_std_p = np.std(X_pruned, axis=0)
    X_std_p[X_std_p == 0] = 1.0
    X_scaled_p = (X_pruned - X_mean_p) / X_std_p
    
    U_p, S_p, Vt_p = np.linalg.svd(X_scaled_p, full_matrices=False)
    loadings_pruned = Vt_p.T
    ev_p = (S_p ** 2) / (N - 1)
    ev_ratio_pruned = ev_p / np.sum(ev_p)
    X_projected_pruned = np.dot(X_scaled_p, loadings_pruned)
    
    plt.figure(figsize=(10, 8))
    
    scatter_pruned = plt.scatter(
        X_projected_pruned[:, 0],
        X_projected_pruned[:, 1],
        c=energy_values,
        cmap="viridis",
        alpha=0.65,
        edgecolors='none',
        s=30
    )
    
    # Draw eigenvectors loadings
    scale_factor = 3.5
    for i, feature in enumerate(active_features):
        plt.arrow(
            0, 0, 
            loadings_pruned[i, 0] * scale_factor, 
            loadings_pruned[i, 1] * scale_factor, 
            color='#FF4081', 
            alpha=0.9, 
            width=0.03, 
            head_width=0.15
        )
        plt.text(
            loadings_pruned[i, 0] * scale_factor * 1.15, 
            loadings_pruned[i, 1] * scale_factor * 1.15, 
            friendly_names_map[feature], 
            color='#FF4081', 
            ha='center', 
            va='center',
            fontsize=9,
            weight='bold'
        )
        
    cbar_pruned = plt.colorbar(scatter_pruned)
    cbar_pruned.set_label("Energy (Raw Metric)", color='white')
    cbar_pruned.ax.yaxis.set_tick_params(color='white')
    plt.setp(plt.getp(cbar_pruned.ax.axes, 'yticklabels'), color='#B3B3B3')
    
    plt.axhline(0, color='#333333', linestyle='--', linewidth=0.8)
    plt.axvline(0, color='#333333', linestyle='--', linewidth=0.8)
    
    plt.xlabel(f"PC1 (variance: {ev_ratio_pruned[0]*100:.2f}%)")
    plt.ylabel(f"PC2 (variance: {ev_ratio_pruned[1]*100:.2f}%)")
    plt.title("Acoustic Principal Component Space (Pruned - Cleaved)", pad=20, color='white', weight='bold')
    plt.tight_layout()
    
    pca_pruned_path = os.path.join(output_dir, "pca_scatter_pruned.png")
    plt.savefig(pca_pruned_path, dpi=300, facecolor='#121212')
    plt.close()
    print(f"-> Saved: {pca_pruned_path}")
    print("\nVisualizations complete!")

def main():
    import tempfile
    import zipfile
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    states_dir = os.path.join(script_dir, "analyzed_states")
    
    print("=" * 80)
    print("              STREAMRIP APP: PRINCIPAL COMPONENT ANALYSIS REPORT             ")
    print("=" * 80)
    
    # 1. Locate the most recent state file in analyzed_states
    zip_path = None
    if os.path.exists(states_dir):
        zip_files = [f for f in os.listdir(states_dir) if f.endswith(".zip")]
        if zip_files:
            zip_files.sort()  # Alphabetical sort aligns with chronological order due to YYYYMMDD_HHMMSS pattern
            zip_path = os.path.join(states_dir, zip_files[-1])
            print(f"\nFound most recent analyzed state zip: {os.path.basename(zip_path)}")
    
    if zip_path:
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                db_temp_path = os.path.join(temp_dir, "library.db")
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    # Verify library.db exists in the archive
                    if "library.db" not in zip_ref.namelist():
                        print(f"Error: library.db not found in {os.path.basename(zip_path)}")
                        return
                    zip_ref.extract("library.db", temp_dir)
                
                print(f"Extracted database to temporary path for analysis...")
                X_raw, metadata = load_data(db_temp_path)
                if len(X_raw) == 0:
                    print("Error: No tracks with features found in database.")
                    return
                    
                X_pct = compute_percentiles(X_raw)
                print_markdown_report(os.path.basename(zip_path), db_temp_path, X_raw, X_pct, metadata)
                
                # Dynamic visual plotting
                ev_raw, loadings_raw, _ = run_pca(X_raw, standardize=True)
                plot_visualizations(os.path.basename(zip_path), X_raw, loadings_raw, ev_raw, script_dir)
                
                print("\n" + "="*80)
        except Exception as e:
            print(f"Failed to analyze state {os.path.basename(zip_path)}: {e}")
            import traceback
            traceback.print_exc()
    else:
        print(f"\nNo state zip files found in {states_dir}")
        print("Checking fallback active databases...")
        
        databases = {
            "HOME (Active User Database)": "/Users/chrismitsacopoulos/library.db",
            "Workspace Bundle (Reference Library)": "/Users/chrismitsacopoulos/Desktop/Mai-An-Lab/tools/offload_cache/bundle/library.db"
        }
        
        for db_name, db_path in databases.items():
            if not os.path.exists(db_path):
                print(f"\nSkipping {db_name} - not found at {db_path}")
                continue
                
            print(f"\nAnalyzing database: {db_name}...")
            try:
                X_raw, metadata = load_data(db_path)
                if len(X_raw) == 0:
                    continue
                    
                X_pct = compute_percentiles(X_raw)
                print_markdown_report(db_name, db_path, X_raw, X_pct, metadata)
                
                # Dynamic visual plotting
                ev_raw, loadings_raw, _ = run_pca(X_raw, standardize=True)
                plot_visualizations(db_name, X_raw, loadings_raw, ev_raw, script_dir)
                
                print("\n" + "="*80)
            except Exception as e:
                print(f"Failed to analyze {db_name}: {e}")

if __name__ == "__main__":
    main()
