import sys
import os
import shutil
import asyncio
import numpy as np

# Ensure StreamripApp working directory is in python search path
sys.path.insert(0, os.getcwd())

from utils.db_manager import DatabaseManager
from utils.dsp import FEATURES_VERSION, unpack_embedding_groups
from utils import track_graph as tg
from utils import taste_model as tm

# 1. Main Async Simulation Task
async def run_simulation():
    print("=" * 80)
    print("      MULTI-SEED SIMILARITY WALK MULTI-CASE VALIDATION SUITE")
    print("=" * 80)

    # A. Sandboxing the database
    prod_db = "/Users/chrismitsacopoulos/Desktop/Mai-An-Lab/tools/offload_cache/bundle/library.db"
    test_db = "test_library.db"

    # Clean up any leftover stale database files first to avoid WAL conflicts
    for suffix in ("", "-wal", "-shm"):
        p = test_db + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

    print(f"[!] Sandboxing: Copying '{prod_db}' to '{test_db}'...")
    shutil.copy(prod_db, test_db)

    db = DatabaseManager(test_db)
    await db.initialize()

    # B. Fetch tracks and read metadata
    rows = await db.get_tracks_with_features(FEATURES_VERSION)
    if not rows:
        print("[Error] No analyzed tracks found in the reference library database!")
        await db.close()
        return

    print(f"    - Loaded {len(rows)} analyzed tracks in test catalog.")

    track_metadata = {}
    for r in rows:
        path = r["path"]
        track_metadata[path] = {
            "title": r.get("title") or os.path.basename(path),
            "artist": r.get("artist") or "Unknown",
            "album": r.get("album") or "Unknown",
            "bpm": float(r.get("bpm", 0) or 0.0),
            "brightness": float(r.get("brightness", 0) or 0.0),
            "energy": float(r.get("energy", 0) or 0.0),
            "rolloff": float(r.get("rolloff", 0) or 0.0),
            "beat_strength": float(r.get("beat_strength", 0) or 0.0),
            "spectral_flatness": float(r.get("spectral_flatness", 0) or 0.0),
            "spectral_contrast": float(r.get("spectral_contrast", 0) or 0.0),
            "key_index": int(r.get("key_index", 0) or 0),
        }

    # C. Perform PCA with Pruning (Production 8 Features Baseline)
    # Using only production features (excluding the noisy chroma entropy from SVD space)
    feature_names = [
        "bpm", "brightness", "energy", "rolloff", "beat_strength", 
        "spectral_flatness", "spectral_contrast", "key_mode"
    ]
    N = len(rows)
    D = len(feature_names)
    X = np.zeros((N, D), dtype=np.float32)

    for idx, r in enumerate(rows):
        path = r["path"]
        meta = track_metadata[path]
        for f_idx, f in enumerate(feature_names):
            if f == "key_mode":
                ki = meta["key_index"]
                X[idx, f_idx] = 1.0 if ki < 12 else 0.0
            else:
                X[idx, f_idx] = meta[f]

    # Compute scaling factors (mean and std dev)
    means = np.mean(X, axis=0)
    stds = np.std(X, axis=0)
    stds[stds == 0] = 1.0
    X_scaled = (X - means) / stds

    # Step 1: First-pass PCA SVD to calculate initial variance loadings
    U, S, Vt = np.linalg.svd(X_scaled, full_matrices=False)
    eigenvalues = (S ** 2) / (N - 1)
    loadings = Vt.T

    # Compute Pearson Correlation Matrix
    corr_matrix = np.corrcoef(X_scaled.T)

    # Run the pruning/cleaving filter at |r| >= 0.85
    redundant = set()
    for i in range(len(feature_names)):
        feat_i = feature_names[i]
        for j in range(i):
            feat_j = feature_names[j]
            if feat_j in redundant:
                continue
            r = abs(corr_matrix[i, j])
            if r >= 0.85:
                redundant.add(feat_i)
                break

    # Step 2: Second-pass PCA SVD on active features only
    active_indices = [idx for idx in range(D) if feature_names[idx] not in redundant]
    X_pruned = X[:, active_indices]
    means_p = np.mean(X_pruned, axis=0)
    stds_p = np.std(X_pruned, axis=0)
    stds_p[stds_p == 0] = 1.0
    X_scaled_p = (X_pruned - means_p) / stds_p

    U_p, S_p, Vt_p = np.linalg.svd(X_scaled_p, full_matrices=False)
    eigenvalues_p = (S_p ** 2) / (N - 1)

    # Assemble 3D coordinates using zero-padding for pruned features
    V_keep = np.zeros((D, 3), dtype=np.float32)
    for active_seq_idx, active_raw_idx in enumerate(active_indices):
        loadings_p_clean = Vt_p[:3, :].T
        V_keep[active_raw_idx, :] = loadings_p_clean[active_seq_idx, :]

    # Project and update DB
    projected_coords = []
    for idx, r in enumerate(rows):
        x = X[idx, :]
        x_scaled = (x - means) / stds
        z = np.dot(x_scaled, V_keep)
        projected_coords.append((r["path"], z))
        track_metadata[r["path"]]["pca_coords"] = z

    await db.update_tracks_pca_coords_batch(projected_coords)
    print("[!] PCA Matrix: Rebuilt and cached clean 3D coordinates (brightness/rolloff pruned dynamically).")

    # D. Construct the neighbor graph edges dynamically for our test DB
    print("[!] Graph Construction: Building sparse neighbor graph edges...")
    acoustic_cnt = await tg.build_acoustic_edges(db)
    artist_cnt, album_cnt = await tg.build_metadata_edges(db)
    print(f"    - Wrote {acoustic_cnt} acoustic edges and {artist_cnt} artist + {album_cnt} album metadata edges.")

    # E. Train dynamic taste model
    # Simulate a user who likes high-energy, heavy rock/metal tracks, and dislikes pop
    # We load seed coordinate examples to train
    w, b = tm.fresh()
    
    metal_paths = [p for p, m in track_metadata.items() if "Black Label Society" in m["artist"] or "Slipknot" in m["artist"]]
    pop_paths = [p for p, m in track_metadata.items() if "Calvin Harris" in m["artist"]]

    # Collect training samples
    pos_samples = [track_metadata[p]["pca_coords"] for p in metal_paths[:3]]
    neg_samples = [track_metadata[p]["pca_coords"] for p in pop_paths[:3]]

    train_size = min(len(pos_samples), len(neg_samples), 3)
    if train_size > 0:
        for i in range(train_size):
            w, b = tm.online_update(np.array(pos_samples[i], dtype=np.float32), 1, w, b, sample_weight=tm.WEIGHT_EXPLICIT, n_samples=2*i + 1)
            w, b = tm.online_update(np.array(neg_samples[i], dtype=np.float32), 0, w, b, sample_weight=tm.WEIGHT_EXPLICIT, n_samples=2*i + 2)
        print(f"[!] Trained Taste Model (Prefers Metal over Pop): w = {np.round(w, 4).tolist()}, b = {b:.4f}")
    else:
        # Fallback to cold start if artists aren't in this catalog
        print("[!] Taste Model: Cold start (neutral weights).")

    # Save to test database and invalidate cache
    weights_bytes = tm.pack_weights(w)
    await db.save_taste_model(weights_bytes, b, train_size, train_size, FEATURES_VERSION)
    tg.invalidate_taste_cache()

    # F. Run Walks across Multiple Seeds (Different Genres)
    print("\n" + "=" * 90)
    print("   GENRE-COHERENCE VALIDATION SUITE: MULTI-SEED SIMILARITY PLAYS")
    print("=" * 90)

    # Let's find distinct seed paths
    seeds = []
    
    # 1. Metal seed
    metal_seed = next((p for p, m in track_metadata.items() if "Black Label Society" in m["artist"]), None)
    if metal_seed:
        seeds.append(("CASE 1: HEAVY METAL SEED", metal_seed))
    
    # 2. Pop/Electronic seed
    pop_seed = next((p for p, m in track_metadata.items() if "Calvin Harris" in m["artist"]), None)
    if pop_seed:
        seeds.append(("CASE 2: DANCE/POP SEED", pop_seed))
        
    # 3. Alternative Rock/Acoustic seed
    alt_seed = next((p for p, m in track_metadata.items() if "Chevelle" in m["artist"] or "Franz Ferdinand" in m["artist"]), None)
    if alt_seed:
        seeds.append(("CASE 3: ALTERNATIVE ROCK SEED", alt_seed))

    # Add fallback seeds if catalog differs
    if not seeds:
        seeds = [("CASE 1 (Catalog Fallback)", rows[0]["path"]), ("CASE 2 (Catalog Fallback)", rows[min(len(rows)-1, 50)]["path"])]

    for title, seed_path in seeds:
        seed_meta = track_metadata[seed_path]
        print(f"\n⚡ {title}")
        print(f"   Seed Track: '{seed_meta['title']}' by {seed_meta['artist']}")
        print("-" * 116)
        print(f"{'Step':<5} | {'Track Title':<30} | {'Artist':<22} | {'P(like)':<9} | {'BPM':<6} | {'Energy':<7} | {'Transition Cost':<15}")
        print("-" * 116)

        # Run Personalized PageRank Walk (Long-Flow, Gentle-Reset in Action)
        walk_paths = await tg.walk(
            db,
            seed_path,
            length=10,
            edge_kinds=(tg.KIND_ACOUSTIC, tg.KIND_ARTIST),
            teleport_path=seed_path,
            taste_weight=0.3, # Enable taste model nudge
        )

        prev_pc = seed_meta["pca_coords"]
        for idx, path in enumerate(walk_paths):
            meta = track_metadata.get(path)
            if not meta:
                continue
            pc = meta["pca_coords"]
            
            # Calculate P(like) prediction
            z_like = float(np.dot(w, pc)) + b
            p_like = 1.0 / (1.0 + np.exp(-max(-30.0, min(30.0, z_like))))

            # Calculate feature distance from previous track
            dist = float(np.linalg.norm(np.array(pc) - np.array(prev_pc)))

            # Mark step 6 to show gentle reset jump clearly
            step_label = f"#{idx+1}"
            if (idx + 1) % 6 == 0:
                step_label += " [RESET]"

            print(f"{step_label:<5} | {meta['title'][:30]:<30} | {meta['artist'][:22]:<22} | {p_like*100:>7.1f}% | {meta['bpm']:>6.1f} | {meta['energy']:>7.3f} | {dist:>15.4f}")
            prev_pc = pc

        print("-" * 116)

    # Cleanup test database
    await db.close()
    try:
        os.remove(test_db)
        print("[!] Cleanup: Removed temporary sandboxed database.")
    except Exception:
        pass

    print("=" * 90)

# Entry point
if __name__ == "__main__":
    asyncio.run(run_simulation())
