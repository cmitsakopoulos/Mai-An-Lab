"""DEV ONLY: give unanalysed tracks fake DSP features so auto-play can be
exercised on a machine that never ran the analyser (e.g. the desktop build).

    python tools/dev_fake_features.py apply  [--db PATH]
    python tools/dev_fake_features.py revert [--db PATH]

apply   snapshots the DB (SQLite online backup, safe while the app runs) to
        <db>.pre-fake.bak, then writes random but artist-clustered sound
        profiles (same artist -> nearby vectors, so "similar" looks plausible)
        for every track that has no current features. Restart the app: its
        startup graph build turns the features into walk geometry.
revert  QUIT THE APP FIRST. Restores the snapshot, discarding everything
        written since apply (fake features, the geometry built from them, and
        any plays/playlists made meanwhile).

Never run the real analyser on a faked DB: it only processes tracks WITHOUT
features, so the fake ones would never be replaced. Revert first.
"""
import argparse
import hashlib
import os
import shutil
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.dsp import EMBED_DIMS, FEATURES_VERSION  # noqa: E402

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", ".flet", "storage", "data", "library.db")


def _vec(artist: str, path: str) -> np.ndarray:
    def rng(key):
        return np.random.default_rng(int(hashlib.sha1(key.encode()).hexdigest()[:12], 16))
    centre = rng("artist:" + (artist or "?")).normal(size=EMBED_DIMS)
    return (centre + 0.35 * rng("track:" + path).normal(size=EMBED_DIMS)).astype("<f4")


def apply(db: str) -> None:
    bak = db + ".pre-fake.bak"
    if os.path.exists(bak):
        sys.exit(f"{bak} already exists — revert (or delete it) before applying again.")
    src = sqlite3.connect(db)
    with sqlite3.connect(bak) as dst:
        src.backup(dst)
    rows = src.execute(
        """SELECT t.path, COALESCE(ar.name, '') FROM tracks t
           LEFT JOIN albums al ON al.id = t.album_id
           LEFT JOIN artists ar ON ar.id = al.artist_id
           LEFT JOIN play_counts pc ON pc.track_path = t.path
           WHERE pc.timbre IS NULL OR COALESCE(pc.features_version, 0) < ?""",
        (FEATURES_VERSION,),
    ).fetchall()
    for path, artist in rows:
        r = np.random.default_rng(int(hashlib.sha1(path.encode()).hexdigest()[:12], 16))
        e, b, ro, bs, fl, sc = r.uniform(0.2, 0.9, 6)
        src.execute(
            """INSERT INTO play_counts
                   (track_path, count, last_played, bpm, energy, brightness, rolloff,
                    beat_strength, spectral_flatness, spectral_contrast, key_index,
                    timbre, features_version)
               VALUES (?, 0, strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(track_path) DO UPDATE SET
                   bpm=excluded.bpm, energy=excluded.energy, brightness=excluded.brightness,
                   rolloff=excluded.rolloff, beat_strength=excluded.beat_strength,
                   spectral_flatness=excluded.spectral_flatness,
                   spectral_contrast=excluded.spectral_contrast, key_index=excluded.key_index,
                   timbre=excluded.timbre, features_version=excluded.features_version,
                   pca_coords=NULL""",
            (path, float(r.uniform(70, 170)), e, b, ro, bs, fl, 0.2 + 0.2 * sc,
             int(r.integers(0, 24)), _vec(artist, path).tobytes(), FEATURES_VERSION),
        )
    src.commit()
    src.close()
    print(f"Faked features for {len(rows)} tracks. Snapshot: {bak}")
    print("Restart the app; it builds the walk geometry ~5 s after startup.")


def revert(db: str) -> None:
    bak = db + ".pre-fake.bak"
    if not os.path.exists(bak):
        sys.exit(f"No snapshot at {bak}.")
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except FileNotFoundError:
            pass
    shutil.move(bak, db)
    print(f"Restored {db} from the pre-fake snapshot.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("apply", "revert"))
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()
    db = os.path.abspath(a.db)
    if not os.path.exists(db):
        sys.exit(f"No database at {db}")
    (apply if a.action == "apply" else revert)(db)
