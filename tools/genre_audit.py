#!/usr/bin/env python3
"""
genre_audit.py — Offline audit of genre metrics and label consistency.

Usage:
    python tools/genre_audit.py <path_to_library.db>
"""

import sys
import os
import sqlite3
import numpy as np
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))
from utils.pca_engine import genre_bucket, genre_tokens, _GENRE_RULES
from utils.genre_eval import jaccard, knn_purity, knn_purity_z, token_jaccard_agreement

OLD_RULES = [
    ("Classical",  ("classical", "classique")),
    ("Hip-Hop",    ("rap", "hip hop", "hip-hop", "hiphop", "trap")),
    ("Electronic", ("électron", "electron", "house", "techno", "dance", "edm", "trance")),
    ("Folk/Cntry", ("folk", "country", "blues", "bluegrass", "americana")),
    ("Soul/R&B",   ("soul", "r&b", "funk", "rnb", "motown")),
    ("Metal",      ("metal", "hard rock", "grunge")),
    ("Rock/Alt",   ("rock", "alternatif", "alternative", "indé", "indie", "punk", "new wave")),
    ("Pop",        ("pop",)),
]

def old_genre_bucket(genre: str | None) -> str:
    g = (genre or "").strip().lower()
    if not g:
        return "Unknown"
    for label, keys in OLD_RULES:
        if any(k in g for k in keys):
            return label
    return "Other"

def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/genre_audit.py <path_to_library.db>")
        sys.exit(1)
        
    db_path = sys.argv[1]
    if not os.path.exists(db_path):
        print(f"Error: file not found at '{db_path}'")
        sys.exit(1)

    # 1. Connect and Load Data
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # artist lives on artists (via albums.artist_id), NOT on tracks.
    sql = """
        SELECT pc.track_path AS path, pc.pca_coords, pc.cluster_id,
               al.genre AS genre, ar.name AS artist
        FROM play_counts pc
        LEFT JOIN tracks   t  ON t.path = pc.track_path
        LEFT JOIN albums   al ON al.id  = t.album_id
        LEFT JOIN artists  ar ON ar.id  = al.artist_id
        WHERE pc.pca_coords IS NOT NULL
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()

    if not rows:
        print("Error: No tracks with PCA coordinates found in database.")
        sys.exit(1)

    print(f"Loaded {len(rows)} tracks from database.")

    # 2. Multilingual Leak recovery
    recovered_count = 0
    for r in rows:
        genre = r["genre"]
        old_b = old_genre_bucket(genre)
        new_b = genre_bucket(genre)
        if old_b in ("Other", "Unknown") and new_b not in ("Other", "Unknown"):
            recovered_count += 1

    print(f"mis-bucketed but recovered by FR/RU tokens: {recovered_count}")

    # 3. Within-artist consistency
    # Group tracks by artist
    artist_tracks = {}
    for r in rows:
        a = r["artist"]
        if a:
            artist_tracks.setdefault(a, []).append(r)

    raw_matches = 0
    bucket_matches = 0
    jaccard_scores = []
    total_pairs = 0

    for artist, tracks in artist_tracks.items():
        n = len(tracks)
        if n < 2:
            continue
        for i in range(n):
            for j in range(i + 1, n):
                g_i = tracks[i]["genre"] or ""
                g_j = tracks[j]["genre"] or ""
                
                # Raw consistency
                if g_i.strip().lower() == g_j.strip().lower():
                    raw_matches += 1
                
                # Bucket consistency
                if genre_bucket(g_i) == genre_bucket(g_j):
                    bucket_matches += 1
                
                # Token-Jaccard consistency
                jaccard_scores.append(jaccard(genre_tokens(g_i), genre_tokens(g_j)))
                total_pairs += 1

    if total_pairs > 0:
        raw_pct = (raw_matches / total_pairs) * 100
        bucket_pct = (bucket_matches / total_pairs) * 100
        avg_jac = np.mean(jaccard_scores)
        print(f"Within-artist consistency: raw {raw_pct:.1f}% -> bucket {bucket_pct:.1f}%; token-Jaccard {avg_jac:.2f}")
    else:
        print("Within-artist consistency: N/A (no artists with multiple tracks)")

    # 4. Metric evaluations (kNN purity, multi-label Jaccard)
    # Parse coordinates
    coords_list = []
    for r in rows:
        c = np.frombuffer(r["pca_coords"], dtype="<f4").astype(np.float64)
        coords_list.append(c)
        
    X = np.stack(coords_list, axis=0)
    N = X.shape[0]

    # Compute Euclidean distance matrix
    sq = (X ** 2).sum(1)
    D = np.sqrt(np.maximum(sq[:, None] - 2 * X @ X.T + sq[None, :], 0.0))
    nbr_order = np.argsort(D, axis=1)

    # Genre single-labels (dominant by global frequency)
    token_sets = [genre_tokens(r["genre"]) for r in rows]
    all_tokens = [tok for tset in token_sets for tok in tset]
    counts = Counter(all_tokens)
    labels = np.array([max(tset, key=lambda t: counts[t]) for tset in token_sets])
    valid_genres = np.array([g != "Unknown" for g in labels])

    # Overall Purity
    purity, purity_null, purity_std, purity_z = knn_purity_z(nbr_order, labels, valid_genres, k=10)
    # Multi-label Jaccard
    jac_mean, jac_null, jac_std, jac_z = token_jaccard_agreement(nbr_order, token_sets, valid_genres, k=10)

    print(f"global kNN purity@10        {purity:.3f}   (null {purity_null:.3f} ± {purity_std:.3f}   z = {purity_z:+.1f})")
    print(f"global multi-label Jaccard  {jac_mean:.3f}   (null {jac_null:.3f} ± {jac_std:.3f}   z = {jac_z:+.1f})")

    # 5. Artist purity (clean anchor)
    artists = np.array([r["artist"] or "Unknown" for r in rows], dtype=object)
    valid_artists = np.array([a != "Unknown" for a in artists])
    if valid_artists.sum() > 3:
        art_purity, art_null, art_std, art_z = knn_purity_z(nbr_order, artists, valid_artists, k=10)
        chance_ratio = art_purity / max(art_null, 1e-12)
        print(f"artist purity@10            {art_purity:.3f}   null {art_null:.3f}           {chance_ratio:.1f}x chance (clean anchor)")
    else:
        print("artist purity@10            N/A (insufficient artist labels)")

    # 6. Fragmentation report (using cluster_id)
    clusters = np.array([r["cluster_id"] if r["cluster_id"] is not None else -1 for r in rows])
    if (clusters != -1).any():
        print("\nfragmentation (communities spanned / biggest-community share):")
        unique_labels = sorted(set(labels[valid_genres]))
        for g in unique_labels:
            m = (labels == g) & valid_genres
            cc = Counter(clusters[m].tolist())
            ncl = len([k for k in cc if k != -1])
            top = max((v for k, v in cc.items() if k != -1), default=0)
            g_count = int(m.sum())
            share = (top / g_count) if g_count > 0 else 0
            print(f"  {g:12s} n={g_count:4d}  in {ncl:3d} communities  biggest={share:.0%}")

if __name__ == "__main__":
    main()
