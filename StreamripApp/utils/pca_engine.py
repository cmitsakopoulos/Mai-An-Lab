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
    Computes PCA on the list of track rows.
    Standardizes the data column-wise (Z-score normalization).
    Applies the Kaiser Criterion (eigenvalue >= 1.0) to determine cutoffs,
    but guarantees a stable 3D projection matrix V_keep to keep the target specs aligned.
    
    Returns:
        means (np.ndarray): 8-element mean vector (mu)
        stds (np.ndarray): 8-element standard deviation vector (sigma)
        V_keep (np.ndarray): 8x3 projection matrix (eigenvectors)
        eigenvalues (np.ndarray): 8 eigenvalues sorted descending
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
    
    # Prevent division by zero for features with zero variance
    stds[stds == 0] = 1.0
    
    # 2. Scale the data (Z-score)
    X_scaled = (X - means) / stds
    
    # 3. Singular Value Decomposition (SVD)
    # X_scaled = U * S * Vt
    try:
        U, S, Vt = np.linalg.svd(X_scaled, full_matrices=False)
    except Exception as e:
        logger.error(f"SVD computation failed: {e}. Falling back.")
        return default_means, default_stds, default_V_keep, default_eigenvalues, 3
        
    # 4. Calculate eigenvalues
    # lambda_i = S_i^2 / (N - 1)
    eigenvalues = (S ** 2) / (N - 1)
    
    # 5. Apply Kaiser Criterion: keep components with eigenvalue >= 1.0
    kaiser_k = int(np.sum(eigenvalues >= 1.0))
    logger.info(f"PCA complete. Kaiser criterion (eigenvalues >= 1.0) selects k={kaiser_k} components.")
    
    # 6. We keep exactly 3 dimensions (PC1, PC2, PC3) to support a stable 3D coordinate space.
    # If the SVD rank is less than 3 (e.g. in small test libraries), we pad Vt with zeros.
    M = Vt.shape[0]
    if M < 3:
        padding = np.zeros((3 - M, D), dtype=np.float32)
        Vt_padded = np.vstack([Vt, padding])
    else:
        Vt_padded = Vt[:3, :]
        
    # Vt has eigenvectors in rows. Vt.T has them in columns.
    # So V_keep is the first 3 columns of Vt.T (first 3 rows of Vt transposed).
    V_keep = Vt_padded.T
    
    # Ensure V_keep is correct float32 type and continuous
    V_keep = np.ascontiguousarray(V_keep, dtype=np.float32)
    
    # Make sure we also return 8 eigenvalues by padding if necessary
    if len(eigenvalues) < D:
        eigenvalues_padded = np.zeros(D, dtype=np.float32)
        eigenvalues_padded[:len(eigenvalues)] = eigenvalues
        eigenvalues = eigenvalues_padded
        
    return means, stds, V_keep, eigenvalues, kaiser_k

def project_track(row: dict, means: np.ndarray, stds: np.ndarray, V_keep: np.ndarray) -> np.ndarray:
    """Projects a single raw track row into the 3D orthogonal PCA space."""
    x = np.array(extract_feature_vector(row), dtype=np.float32)
    # Standardize
    x_scaled = (x - means) / stds
    # Multiply by loadings to project
    z = np.dot(x_scaled, V_keep)
    return z
