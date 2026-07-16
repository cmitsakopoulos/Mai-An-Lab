import sqlite3
import sys
import numpy as np

if len(sys.argv) < 2:
    print("Usage: python migrate_v4_to_v5_drop_delta.py <database_path>")
    sys.exit(1)

N_MFCC = 20
db_path = sys.argv[1]
db = sqlite3.connect(db_path)

# 1. Migrate play_counts table
try:
    rows = db.execute(
        "SELECT track_path, timbre FROM play_counts "
        "WHERE timbre IS NOT NULL AND features_version = 4"
    ).fetchall()
    n = 0
    for path, blob in rows:
        if len(blob) != 88 * 4:  # not a v4 blob, skip
            continue
        v = np.frombuffer(blob, dtype="<f4")
        v5 = np.delete(v, np.s_[2 * N_MFCC:3 * N_MFCC]).astype("<f4").tobytes()  # drop [40:60)
        db.execute("UPDATE play_counts SET timbre=?, features_version=5 WHERE track_path=?",
                   (v5, path))
        n += 1
    db.commit()
    print(f"Migrated {n} tracks in play_counts to v5 ({db_path})")
except sqlite3.OperationalError:
    pass

# 2. Migrate feature_cache table
try:
    rows = db.execute(
        "SELECT track_path, timbre FROM feature_cache "
        "WHERE timbre IS NOT NULL AND features_version = 4"
    ).fetchall()
    n = 0
    for path, blob in rows:
        if len(blob) != 88 * 4:  # not a v4 blob, skip
            continue
        v = np.frombuffer(blob, dtype="<f4")
        v5 = np.delete(v, np.s_[2 * N_MFCC:3 * N_MFCC]).astype("<f4").tobytes()  # drop [40:60)
        db.execute("UPDATE feature_cache SET timbre=?, features_version=5 WHERE track_path=?",
                   (v5, path))
        n += 1
    db.commit()
    print(f"Migrated {n} tracks in feature_cache to v5 ({db_path})")
except sqlite3.OperationalError:
    pass

db.close()
