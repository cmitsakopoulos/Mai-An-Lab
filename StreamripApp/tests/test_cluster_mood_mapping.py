"""Independent test script to evaluate cluster-to-mood mapping.

Invoked manually:
    python3 tests/test_cluster_mood_mapping.py
"""

from __future__ import annotations

import asyncio
import glob
import os
import sys
import tempfile
import zipfile
import shutil
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Override APP_DIR so we don't pollute persistent state
from utils import config
config.APP_DIR = tempfile.mkdtemp(prefix="test_cluster_mood_")

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


# Define Target Percentile Profiles for standard moods
MOOD_TARGETS = {
    "chill": {
        "bpm": 0.25,
        "energy": 0.20,
        "beat_strength": 0.30,
        "brightness": 0.30,
        "spectral_flatness": 0.50,
        "spectral_contrast": 0.30,
        "rolloff": 0.30,
        "key_mode": 0.20
    },
    "dark": {
        "bpm": 0.35,
        "energy": 0.25,
        "beat_strength": 0.40,
        "brightness": 0.20,
        "spectral_flatness": 0.40,
        "spectral_contrast": 0.50,
        "rolloff": 0.25,
        "key_mode": 0.10
    },
    "upbeat": {
        "bpm": 0.75,
        "energy": 0.80,
        "beat_strength": 0.75,
        "brightness": 0.80,
        "spectral_flatness": 0.60,
        "spectral_contrast": 0.60,
        "rolloff": 0.75,
        "key_mode": 0.85
    },
    "beats": {
        "bpm": 0.40,
        "energy": 0.60,
        "beat_strength": 0.85,
        "brightness": 0.50,
        "spectral_flatness": 0.50,
        "spectral_contrast": 0.70,
        "rolloff": 0.55,
        "key_mode": 0.30
    },
    "intense": {
        "bpm": 0.85,
        "energy": 0.90,
        "beat_strength": 0.80,
        "brightness": 0.70,
        "spectral_flatness": 0.40,
        "spectral_contrast": 0.80,
        "rolloff": 0.80,
        "key_mode": 0.20
    },
    "rock": {
        "bpm": 0.60,
        "energy": 0.70,
        "beat_strength": 0.65,
        "brightness": 0.60,
        "spectral_flatness": 0.50,
        "spectral_contrast": 0.60,
        "rolloff": 0.60,
        "key_mode": 0.50
    }
}


async def main() -> None:
    bundle = _resolve_bundle()
    print(f"Using bundle: {bundle}")
    extract_dir = _extract(bundle)
    work_db = os.path.join(extract_dir, "work.db")
    shutil.copy2(os.path.join(extract_dir, "library.db"), work_db)

    db = DatabaseManager(work_db)
    
    # 1. Build metadata and acoustic edges (so that K-Means clustering runs)
    print("\n[1/4] Rebuilding Graph edges & clusters...")
    await tg.build_metadata_edges(db)
    await tg.build_acoustic_edges(db)

    # 2. Fetch tracks and clusters
    rows = await db.get_tracks_with_features(tg.FEATURES_VERSION)
    N = len(rows)
    print(f"Loaded {N} tracks with features.")

    cluster_bulk = await db.get_track_clusters_bulk([r["path"] for r in rows])
    
    # Group tracks by cluster
    clusters: dict[int, list[dict]] = {}
    for r in rows:
        cid = cluster_bulk.get(r["path"])
        if cid is not None:
            clusters.setdefault(cid, []).append(r)

    print(f"Found {len(clusters)} unique clusters.")

    # 3. Calculate percentile ranks in memory for all features
    raw_features = ["bpm", "brightness", "energy", "rolloff", "beat_strength", "spectral_flatness", "spectral_contrast", "key_mode"]
    track_ranks = {}
    for f in raw_features:
        if f == "key_mode":
            vals = []
            for r in rows:
                ki = r.get("key_index", 0) or 0
                cam = key_index_to_camelot(ki)
                km = 1.0 if cam and cam[1] == "B" else 0.0
                vals.append(km)
            vals = np.array(vals, dtype=np.float32)
        else:
            vals = np.array([float(r.get(f) or 0.0) for r in rows], dtype=np.float32)
        ranks = np.argsort(np.argsort(vals)) / max(1, N - 1)
        track_ranks[f] = {rows[idx]["path"]: float(ranks[idx]) for idx in range(N)}

    # 4. Profile each cluster (median percentile for scalars, mean for binary key_mode)
    cluster_profiles: dict[int, dict[str, float]] = {}
    print("\n[2/4] Profiling cluster centroids...")
    for cid, tracks in sorted(clusters.items()):
        profile = {}
        for f in raw_features:
            vals = [track_ranks[f][t["path"]] for t in tracks if t["path"] in track_ranks[f]]
            if f == "key_mode":
                # Average of raw major/minor flag (gives major proportion)
                raw_keys = []
                for t in tracks:
                    ki = t.get("key_index", 0) or 0
                    cam = key_index_to_camelot(ki)
                    km = 1.0 if cam and cam[1] == "B" else 0.0
                    raw_keys.append(km)
                profile[f] = float(np.mean(raw_keys)) if raw_keys else 0.0
            else:
                profile[f] = float(np.median(vals)) if vals else 0.5
        cluster_profiles[cid] = profile

        # Print some summary stats for each cluster
        first_few = tracks[:3]
        samples = ", ".join(f"'{t.get('artist')} - {t.get('title')}'" for t in first_few)
        print(f"  Cluster {cid:>2} (size={len(tracks):>3}): bpm_pct={profile['bpm']:.2f}, energy_pct={profile['energy']:.2f}, major_ratio={profile['key_mode']:.2%}")
        print(f"             Samples: {samples}")

    # 5. Map clusters to moods using Euclidean distance in Percentile Space
    print("\n[3/4] Mapping clusters to moods...")
    mood_assignments: dict[str, list[tuple[int, float]]] = {m: [] for m in MOOD_TARGETS}
    
    for cid, profile in cluster_profiles.items():
        best_mood = None
        min_dist = float("inf")
        
        # Calculate distance to each mood preset
        for m, target in MOOD_TARGETS.items():
            dist_sq = 0.0
            for f in raw_features:
                dist_sq += (profile[f] - target[f]) ** 2
            dist = float(np.sqrt(dist_sq))
            
            # Store soft matches
            mood_assignments[m].append((cid, dist))

    # Sort cluster assignments for each mood by distance
    for m in mood_assignments:
        mood_assignments[m].sort(key=lambda x: x[1])

    # 6. Report the mappings and representative tracks
    print("\n[4/4] Final Mood-to-Cluster Mapping Report:")
    print("=" * 80)
    for m, cids in mood_assignments.items():
        print(f"\nMOOD: {m.upper()} (Target: bpm={MOOD_TARGETS[m]['bpm']:.2f}, energy={MOOD_TARGETS[m]['energy']:.2f}, major_ratio={MOOD_TARGETS[m]['key_mode']:.2%})")
        print("-" * 50)
        # Show top 2 matching clusters
        for cid, dist in cids[:2]:
            tracks = clusters[cid]
            print(f"  -> MATCHES Cluster {cid:>2} (distance={dist:.3f}, size={len(tracks)} tracks)")
            # Show a few sample tracks from this cluster
            print("     Sample tracks in cluster:")
            # Sort tracks by distance to cluster's median profile for better representation
            track_dists = []
            for t in tracks:
                tdist_sq = 0.0
                for f in raw_features:
                    tdist_sq += (track_ranks[f].get(t["path"], 0.5) - cluster_profiles[cid][f]) ** 2
                track_dists.append((t, np.sqrt(tdist_sq)))
            track_dists.sort(key=lambda x: x[1])
            
            for t, t_dist in track_dists[:4]:
                ki = t.get("key_index", 0) or 0
                print(f"       * {t.get('artist')} - {t.get('title')} (BPM: {t.get('bpm'):.0f}, Key: {_camelot_str(ki)}, dist={t_dist:.3f})")
    print("=" * 80)

    # 7. Write the complete list of all tracks in each cluster to tests/cluster_tracks_report.txt
    report_path = "tests/cluster_tracks_report.txt"
    print(f"\n[5/5] Writing full cluster tracks listing to {report_path}...")
    with open(report_path, "w", encoding="utf-8") as f_out:
        f_out.write("FULL CLUSTER TRACKS REPORT\n")
        f_out.write("=" * 80 + "\n\n")
        for cid, tracks in sorted(clusters.items()):
            profile = cluster_profiles[cid]
            f_out.write(f"CLUSTER {cid} (size = {len(tracks)} tracks)\n")
            f_out.write(f"Profile: bpm_pct={profile['bpm']:.2f}, energy_pct={profile['energy']:.2f}, major_ratio={profile['key_mode']:.2%}\n")
            f_out.write("-" * 80 + "\n")
            
            # Sort tracks alphabetically by artist, then title
            sorted_tracks = sorted(tracks, key=lambda x: (x.get("artist") or "", x.get("title") or ""))
            for t in sorted_tracks:
                ki = t.get("key_index", 0) or 0
                bpm = float(t.get("bpm", 0) or 0)
                f_out.write(f"  * {t.get('artist')} - {t.get('title')} (BPM: {bpm:.0f}, Key: {_camelot_str(ki)})\n")
            f_out.write("\n" + "=" * 80 + "\n\n")
    print("Done writing report.")

    if db._conn is not None:
        await db._conn.close()


if __name__ == "__main__":
    asyncio.run(main())
