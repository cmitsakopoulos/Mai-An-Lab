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
                load_strs.append(f"**{val:+.4f}** 🎯")
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
                load_strs.append(f"**{val:+.4f}** 🎯")
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
                row_cells.append(f"**{val:+.2f}** 🔥")
            else:
                row_cells.append(f"{val:+.2f}")
        print(f"| `{f}` | " + " | ".join(row_cells) + " |")

def main():
    databases = {
        "HOME (Active User Database)": "/Users/chrismitsacopoulos/library.db",
        "Workspace Bundle (Reference Library)": "/Users/chrismitsacopoulos/Desktop/Mai-An-Lab/tools/offload_cache/bundle/library.db"
    }
    
    print("=" * 80)
    print("              STREAMRIP APP: PRINCIPAL COMPONENT ANALYSIS REPORT             ")
    print("=" * 80)
    
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
            print("\n" + "="*80)
        except Exception as e:
            print(f"Failed to analyze {db_name}: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()
