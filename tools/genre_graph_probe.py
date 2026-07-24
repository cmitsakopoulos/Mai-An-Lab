"""Read-only validation of utils.genre_graph against a real library image.

Loads Zr coords + per-track genre/country straight from a library.db, runs the
production `build_genre_graph`, and prints the regional-aware node sizes, the
PAGA adjacency, and a few example journey queues. This is the productionised
form of the ad-hoc scratchpad probe: it exercises the SHIPPING code paths, so a
divergence here is a regression, not a probe artifact.

Usage:
    python tools/genre_graph_probe.py /path/to/library.db

Never writes. Point it at a COPY of a state-backup image, e.g.
    unzip tools/state_backups/<image>.zip -d /tmp/img
    python tools/genre_graph_probe.py /tmp/img/library.db
"""
import json
import os
import sqlite3
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "StreamripApp"))

from utils import genre_graph as gg  # noqa: E402


def load(db_path):
    c = sqlite3.connect(db_path)
    rows = c.execute(
        """
        SELECT t.path, pc.pca_coords, t.title, ar.name AS artist,
               ae.genres AS genres, ae.country AS country
        FROM tracks t
        JOIN play_counts pc ON pc.track_path = t.path AND pc.pca_coords IS NOT NULL
        LEFT JOIN albums al ON al.id = t.album_id
        LEFT JOIN artists ar ON ar.id = al.artist_id
        LEFT JOIN artist_enrichment ae ON ae.artist_name = ar.name
        """
    ).fetchall()
    X, meta, titles, artists = [], [], [], []
    for path, blob, title, artist, genres, country in rows:
        X.append(np.frombuffer(blob, dtype=np.float32))
        toks = set()
        if genres:
            try:
                for g in json.loads(genres):
                    tk = "".join(ch for ch in (g.get("name", "") or "").lower() if ch.isalnum())
                    if tk:
                        toks.add(tk)
            except Exception:
                pass
        meta.append({"genres": toks, "country": country})
        titles.append(title or "?")
        artists.append(artist or "?")
    X = np.vstack(X).astype(np.float32)
    X_unit = X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
    return X_unit, meta, titles, artists


def main(db_path):
    X_unit, meta, titles, artists = load(db_path)
    g = gg.build_genre_graph(X_unit, meta, k=15, min_size=10, min_lift=1.0)
    nodes = g["nodes"]
    N = len(nodes)
    n_inf = sum(g["inferred"])
    print(f"tracks={N}  Zr-dim={X_unit.shape[1]}  inferred-by-propagation={n_inf} "
          f"({100*n_inf/N:.1f}%)")

    print("\nnode sizes (regional-aware, post-propagation):")
    for node, sz in g["sizes"].most_common():
        star = "  <- had inferred members" if any(
            g["inferred"][i] and nodes[i] == node for i in range(N)
        ) else ""
        print(f"  {node:16s} {sz:5d}{star}")

    print("\nPAGA adjacency (node -> top neighbours by lift, size>=10):")
    for node in g["adj"]:
        top = ", ".join(f"{b}({l:.2f})" for b, l in g["adj"][node][:3]) or "(no real exit -> radius)"
        print(f"  {node:16s} -> {top}")

    # example journeys from the most-central seed of a few well-populated nodes
    S = X_unit @ X_unit.T
    np.fill_diagonal(S, -np.inf)

    def central(node):
        members = [i for i in range(N) if nodes[i] == node]
        if not members:
            return None
        sub = S[np.ix_(members, members)]
        sub = np.where(np.isinf(sub), -np.inf, sub)
        return members[int(np.argmax(sub.sum(1)))]

    wanted = [n for n, _ in g["sizes"].most_common() if g["sizes"][n] >= 10][:5]
    for node in wanted:
        si = central(node)
        if si is None:
            continue
        out = gg.journey(si, X_unit, nodes, g["adj"], length=8, hops=1)
        print(f"\n--- JOURNEY  seed node = {node} ---")
        for k, i in enumerate(out):
            tag = "SEED" if i == si else "    "
            print(f"  {tag} [{nodes[i]:13s}] {titles[i][:34]:34s} - {artists[i][:22]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
