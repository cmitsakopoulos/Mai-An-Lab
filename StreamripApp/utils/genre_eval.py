import numpy as np

def jaccard(a: set, b: set) -> float:
    """Compute Jaccard similarity between two sets.
    If both are empty, return 1.0."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)

def knn_purity(order, labels, valid, k=10) -> tuple[float, dict[str, tuple[float, int]]]:
    """Compute overall and per-class kNN purity.
    
    order: (N, N) array of neighbor indices sorted by distance.
    labels: (N,) array of class labels.
    valid: (N,) boolean mask of valid items.
    """
    vset = np.asarray(valid)
    labels = np.asarray(labels)
    N = order.shape[0]
    purities = []
    per_label = {}
    for i in range(N):
        if not vset[i]:
            continue
        gi = labels[i]
        found, hit = 0, 0
        for j in order[i]:
            if j == i or not vset[j]:
                continue
            found += 1
            if labels[j] == gi:
                hit += 1
            if found >= k:
                break
        if found:
            p = hit / found
            purities.append(p)
            per_label.setdefault(gi, []).append(p)
    overall = float(np.mean(purities)) if purities else 0.0
    per = {g: (float(np.mean(v)), len(v)) for g, v in per_label.items()}
    return overall, per

def knn_purity_z(order, labels, valid, k=10, B=200, seed=0) -> tuple[float, float, float, float]:
    """Returns (observed, null_mean, null_std, z). Single-label purity vs label-permutation null."""
    observed, _ = knn_purity(order, labels, valid, k)
    rng = np.random.RandomState(seed)
    idx = np.where(valid)[0]
    labs = labels[idx].copy()
    vals = []
    for _ in range(B):
        perm = labs.copy()
        rng.shuffle(perm)
        shuffled = labels.copy()
        shuffled[idx] = perm
        v, _ = knn_purity(order, shuffled, valid, k)
        vals.append(v)
    null_mean = float(np.mean(vals))
    null_std = float(np.std(vals))
    z = (observed - null_mean) / (null_std + 1e-12)
    return observed, null_mean, null_std, z

def per_class_auc(D, labels, valid, min_n=8) -> dict[str, tuple[float, int]]:
    """Base-rate-invariant Mann-Whitney retrieval AUC per genre (intrinsic
    separability, independent of class imbalance)."""
    idx_valid = np.where(valid)[0]
    valid_labels = labels[idx_valid]
    genres = sorted(set(valid_labels))
    
    res = {}
    for g in genres:
        g_mask = (labels == g) & valid
        other_mask = (~(labels == g)) & valid
        
        idx_g = np.where(g_mask)[0]
        idx_other = np.where(other_mask)[0]
        
        n_g = len(idx_g)
        if n_g < min_n:
            continue
            
        aucs = []
        for i in idx_g:
            pos_dists = D[i, idx_g[idx_g != i]]
            neg_dists = D[i, idx_other]
            
            if len(pos_dists) == 0 or len(neg_dists) == 0:
                continue
            
            # Broadcast comparison of all positives vs all negatives for query i
            comp = pos_dists[:, None] < neg_dists[None, :]
            eq = pos_dists[:, None] == neg_dists[None, :]
            u = np.sum(comp) + 0.5 * np.sum(eq)
            auc_i = u / (len(pos_dists) * len(neg_dists))
            aucs.append(auc_i)
            
        if aucs:
            res[g] = (float(np.mean(aucs)), n_g)
    return res

def token_jaccard_agreement(order, token_sets, valid, k=10, B=200, seed=0) -> tuple[float, float, float, float]:
    """Multi-label neighbour agreement vs permutation null. Returns (observed, null_mean, null_std, z)."""
    N = order.shape[0]
    valid_mask = np.asarray(valid)
    
    def compute_mean_agreement(tok_sets):
        agreements = []
        for i in range(N):
            if not valid_mask[i]:
                continue
            found = []
            for j in order[i]:
                if j == i or not valid_mask[j]:
                    continue
                found.append(j)
                if len(found) >= k:
                    break
            if found:
                set_i = tok_sets[i]
                jacs = [jaccard(set_i, tok_sets[j]) for j in found]
                agreements.append(np.mean(jacs))
        return float(np.mean(agreements)) if agreements else 0.0

    observed = compute_mean_agreement(token_sets)
    
    rng = np.random.RandomState(seed)
    idx = np.where(valid_mask)[0]
    
    tok_arr = np.empty(N, dtype=object)
    for idx_i, s in enumerate(token_sets):
        tok_arr[idx_i] = s
        
    vals = []
    for _ in range(B):
        perm_sets = tok_arr.copy()
        shuffled_indices = idx.copy()
        rng.shuffle(shuffled_indices)
        perm_sets[idx] = tok_arr[shuffled_indices]
        vals.append(compute_mean_agreement(perm_sets))
        
    null_mean = float(np.mean(vals))
    null_std = float(np.std(vals))
    z = (observed - null_mean) / (null_std + 1e-12)
    return observed, null_mean, null_std, z

def compute_silhouette_scores(D, labels, valid) -> tuple[np.ndarray, float, dict[str, tuple[float, int]]]:
    """Compute per-point silhouette scores, global mean, and per-class means.
    
    D: distance matrix (N, N).
    labels: array of labels (N,).
    valid: boolean mask (N,).
    """
    N = D.shape[0]
    idx = np.where(valid)[0]
    labs = labels[idx]
    uniq = sorted(set(labs))
    by = {g: idx[labels[idx] == g] for g in uniq}
    
    sil = np.zeros(N)
    glob = []
    sils = {}
    
    for g in uniq:
        members = by[g]
        s_g = []
        for i in members:
            same = members[members != i]
            a = D[i, same].mean() if same.size else 0.0
            b = min((D[i, by[h]].mean() for h in uniq if h != g and by[h].size),
                    default=0.0)
            s = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
            sil[i] = s
            s_g.append(s)
            glob.append(s)
        sils[g] = (float(np.mean(s_g)) if s_g else 0.0, len(members))
        
    global_mean = float(np.mean(glob)) if glob else 0.0
    return sil, global_mean, sils
