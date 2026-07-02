import os
import sys
import json
import asyncio
import sqlite3
import zipfile
import numpy as np
import matplotlib.pyplot as plt
import umap

# Ensure StreamripApp is in path
app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from utils.track_graph import unpack_graph_embedding, _feature_vector, GRAPH_EMBED_DIMS
from utils.pca_engine import genre_bucket, _GENRE_PALETTE
from utils.db_manager import DatabaseManager

# Preferred color palettes
COUNTRY_PALETTE = {
    "US": "#40C4FF",  # Cyan
    "GB": "#FFD740",  # Amber/Yellow
    "GR": "#FF5252",  # Red
    "FR": "#76FF03",  # Neon Green
    "DE": "#B388FF",  # Purple
    "SE": "#FF80AB",  # Pink
    "CA": "#FF9100",  # Orange
    "AU": "#00E676",  # Emerald Green
    "IT": "#00E5FF",  # Teal
    "JP": "#E040FB",  # Magenta
    "NL": "#FFAB40",  # Orange Accent
    "NO": "#64FFDA",  # Teal Accent
    "FI": "#18FFFF",  # Bright Cyan
    "DK": "#FF5252",  # Red
    "ES": "#FFD600",  # Yellow
    "PT": "#00C853",  # Green
    "IE": "#69F0AE",  # Light Green
    "BR": "#AEEA00",  # Lime Accent
    "AR": "#40C4FF",  # Light Blue
    "MX": "#00E676",  # Green
    "JM": "#FFD740",  # Gold
    "KR": "#FF4081",  # Pink Accent
    "ZA": "#FF6D00",  # Deep Orange
    "Unknown": "#444444", # Grey
}

# Offline artist provenance map for testing when network is offline/rate-limited
MOCK_PROVENANCE = {
    "Panos Kiamos": {"country": "GR", "genres": [{"name": "laiko", "count": 50}, {"name": "laiko pop", "count": 20}]},
    "Giorgos Mazonakis": {"country": "GR", "genres": [{"name": "laiko", "count": 40}]},
    "Konstantinos Argiros": {"country": "GR", "genres": [{"name": "laiko pop", "count": 35}]},
    "Vasilis Karras": {"country": "GR", "genres": [{"name": "laiko", "count": 60}]},
    "Pantelis Pantelidis": {"country": "GR", "genres": [{"name": "laiko", "count": 45}]},
    "Panos Kiamos, OGE": {"country": "GR", "genres": [{"name": "laiko pop", "count": 30}]},
    "A$AP Rocky": {"country": "US", "genres": [{"name": "hip hop", "count": 100}, {"name": "rap", "count": 80}]},
    "Drake": {"country": "CA", "genres": [{"name": "hip hop", "count": 120}, {"name": "rap", "count": 90}]},
    "Future": {"country": "US", "genres": [{"name": "hip hop", "count": 110}, {"name": "trap", "count": 95}]},
    "Playboi Carti": {"country": "US", "genres": [{"name": "hip hop", "count": 85}, {"name": "trap", "count": 70}]},
    "Rick Ross": {"country": "US", "genres": [{"name": "hip hop", "count": 90}]},
    "YoungBoy Never Broke Again": {"country": "US", "genres": [{"name": "hip hop", "count": 75}]},
    "Skepta": {"country": "GB", "genres": [{"name": "grime", "count": 95}, {"name": "hip hop", "count": 60}]},
    "Digga D": {"country": "GB", "genres": [{"name": "uk drill", "count": 80}, {"name": "hip hop", "count": 50}]},
    "Headie One": {"country": "GB", "genres": [{"name": "uk drill", "count": 85}, {"name": "hip hop", "count": 55}]},
    "Mad Clip": {"country": "GR", "genres": [{"name": "trap", "count": 70}, {"name": "hip hop", "count": 65}]},
    "LEX": {"country": "GR", "genres": [{"name": "hip hop", "count": 90}, {"name": "rap", "count": 85}]},
    "Vlospa": {"country": "GR", "genres": [{"name": "rap", "count": 60}, {"name": "hip hop", "count": 55}]},
    "Saske": {"country": "GR", "genres": [{"name": "trap", "count": 65}, {"name": "hip hop", "count": 50}]},
    "Light": {"country": "GR", "genres": [{"name": "trap", "count": 75}, {"name": "hip hop", "count": 60}]},
    "Negros Tou Moria": {"country": "GR", "genres": [{"name": "hip hop", "count": 50}]},
    "Kerri Chandler": {"country": "US", "genres": [{"name": "house", "count": 100}, {"name": "electronic", "count": 80}]},
    "Gaskin": {"country": "GB", "genres": [{"name": "house", "count": 70}, {"name": "electronic", "count": 60}]},
    "Rossi.": {"country": "GB", "genres": [{"name": "house", "count": 75}, {"name": "electronic", "count": 65}]},
    "Charli XCX": {"country": "GB", "genres": [{"name": "electronic", "count": 90}, {"name": "pop", "count": 80}]},
    "Calvin Harris": {"country": "GB", "genres": [{"name": "edm", "count": 110}, {"name": "house", "count": 90}]},
    "Calvin Harris, Kasabian": {"country": "GB", "genres": [{"name": "edm", "count": 80}, {"name": "electronic", "count": 70}]},
    "Calvin Harris, Kasabian": {"country": "GB", "genres": [{"name": "electronic", "count": 75}]},
    "Pink Floyd": {"country": "GB", "genres": [{"name": "rock", "count": 150}, {"name": "progressive rock", "count": 120}]},
    "Creedence Clearwater Revival": {"country": "US", "genres": [{"name": "rock", "count": 130}, {"name": "swamp rock", "count": 90}]},
    "Joy Division": {"country": "GB", "genres": [{"name": "post-punk", "count": 140}, {"name": "new wave", "count": 100}]},
    "Twin Tribes": {"country": "US", "genres": [{"name": "coldwave", "count": 80}, {"name": "post-punk", "count": 75}]},
    "Title Fight": {"country": "US", "genres": [{"name": "post-punk", "count": 70}, {"name": "rock", "count": 65}]},
}


async def enrich_and_normalize_database(db_path: str):
    """Enriches the database with artist provenance & MusicBrainz consensus genres,
    then executes fix_and_normalize_track_genres() to update all album genres."""
    db_manager = DatabaseManager(db_path)
    await db_manager.initialize()

    conn = await db_manager.get_connection()
    
    # 1. Ensure artist_enrichment table exists
    create_table_sql = """
    CREATE TABLE IF NOT EXISTS artist_enrichment (
        artist_name TEXT PRIMARY KEY,
        mbid TEXT,
        country TEXT,
        area TEXT,
        genres TEXT,
        source TEXT DEFAULT 'musicbrainz',
        score INTEGER DEFAULT 100,
        status TEXT DEFAULT 'ok',
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    async with db_manager._write_lock:
        await conn.execute(create_table_sql)
        await conn.commit()

    # 2. Populate artist_enrichment with provenance data for all artists
    async with conn.execute("SELECT name FROM artists") as cur:
        artists = [r["name"] for r in await cur.fetchall()]

    enrichment_records = []
    for artist in artists:
        prov = MOCK_PROVENANCE.get(artist)
        if prov:
            enrichment_records.append((
                artist, "mbid-mock", prov["country"], prov["country"],
                json.dumps(prov["genres"]), "musicbrainz", 100, "ok"
            ))

    if enrichment_records:
        async with db_manager._write_lock:
            await conn.executemany(
                "INSERT OR REPLACE INTO artist_enrichment (artist_name, mbid, country, area, genres, source, score, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                enrichment_records
            )
            await conn.commit()

    # 3. Run full fix_and_normalize_track_genres routine
    summary = await db_manager.fix_and_normalize_track_genres()
    print(f"[UMAP Test] Database Genre Normalization & API Fix Summary: {summary}")
    await db_manager.close()


def prepare_database() -> str:
    """Unpacks the latest phone state export zip or falls back to local bundle."""
    zip_path = os.path.join(app_dir, "..", "tools", "analyzed_states", "mai_an_lab_state_latest.analysed.zip")
    temp_dir = os.path.join(app_dir, "..", "tools", "offload_cache", "umap_test_db")
    os.makedirs(temp_dir, exist_ok=True)

    if os.path.exists(zip_path):
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(temp_dir)
        db_path = os.path.join(temp_dir, "library.db")
        if os.path.exists(db_path):
            return db_path

    # Fallback to local bundle db
    fallback_db = os.path.join(app_dir, "..", "tools", "offload_cache", "bundle", "library.db")
    if os.path.exists(fallback_db):
        return fallback_db
    raise FileNotFoundError("No library database found for UMAP projection test.")


def run_comparative_umap():
    db_path = prepare_database()
    print(f"[UMAP Test] Loading library database: {db_path}")

    # Run full enrichment & MusicBrainz normalization
    asyncio.run(enrich_and_normalize_database(db_path))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = """
    SELECT t.path, t.title, ar.name AS artist_name, al.title AS album_title,
           al.genre AS album_genre, pc.timbre, pc.bpm, pc.brightness, pc.energy,
           pc.rolloff, pc.beat_strength, pc.spectral_flatness, pc.spectral_contrast,
           pc.key_index, e.country AS country_code, e.genres AS api_genres
    FROM tracks t
    JOIN albums al ON al.id = t.album_id
    JOIN artists ar ON ar.id = al.artist_id
    JOIN play_counts pc ON pc.track_path = t.path
    LEFT JOIN artist_enrichment e ON e.artist_name = ar.name
    WHERE pc.timbre IS NOT NULL
    """

    cur.execute(sql)
    rows = cur.fetchall()
    print(f"[UMAP Test] Retrieved {len(rows)} tracks with audio encodings post-enrichment.")

    vectors = []
    genres = []
    countries = []
    track_labels = []

    for r in rows:
        row_dict = dict(r)
        v = unpack_graph_embedding(row_dict.get("timbre"))
        if v is None or v.shape[0] != GRAPH_EMBED_DIMS:
            continue

        # Extract 68-D graph embedding vector
        feat_vec = _feature_vector(row_dict, v, ["bpm", "brightness", "energy", "rolloff", "beat_strength"])
        vectors.append(feat_vec)

        # Genre resolution from enriched albums
        g_bucket = genre_bucket(row_dict.get("album_genre"))
        genres.append(g_bucket)

        # Country resolution from enriched artists
        cty = (row_dict.get("country_code") or "Unknown").upper().strip()
        countries.append(cty)

        track_labels.append(f"{row_dict.get('artist_name')} - {row_dict.get('title')}")

    if len(vectors) < 10:
        print("[UMAP Test] Error: Not enough tracks with valid audio encodings to compute UMAP.")
        return

    X = np.stack(vectors, axis=0)
    print(f"[UMAP Test] Feature Matrix Shape: {X.shape} (Tracks x Audio Feature Dimensions)")

    # Compute UMAP 2D Projection
    print("[UMAP Test] Computing 2D UMAP projection over audio encodings...")
    reducer = umap.UMAP(n_neighbors=15, min_dist=0.1, n_components=2, random_state=42)
    embedding = reducer.fit_transform(X)

    # Plotting Side-by-Side Figure
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 9), facecolor="#0D1117")
    ax1.set_facecolor("#161B22")
    ax2.set_facecolor("#161B22")

    # 1. Subplot 1: Mega-Genre Distribution
    ax1.set_title("Audio Encodings Projected by Enriched Mega-Genre (UMAP)", color="#F0F6FC", fontsize=14, pad=12, fontweight="bold")
    unique_genres = sorted(list(set(genres)), key=lambda g: 0 if g != "Unknown" else 1)
    for g in unique_genres:
        mask = [curr == g for curr in genres]
        color = _GENRE_PALETTE.get(g, "#888888")
        ax1.scatter(
            embedding[mask, 0], embedding[mask, 1],
            c=color, label=f"{g} ({sum(mask)})", s=28, alpha=0.85, edgecolors="none"
        )
    ax1.legend(facecolor="#21262D", edgecolor="#30363D", labelcolor="#F0F6FC", fontsize=9, loc="upper right")
    ax1.tick_params(colors="#8B949E")
    ax1.grid(True, color="#30363D", linestyle="--", linewidth=0.5, alpha=0.5)

    # 2. Subplot 2: Country of Origin Distribution
    ax2.set_title("Audio Encodings Projected by Enriched Country Code (UMAP)", color="#F0F6FC", fontsize=14, pad=12, fontweight="bold")
    unique_countries = sorted(list(set(countries)), key=lambda c: 0 if c not in ("UNKNOWN", "NONE") else 1)
    
    for cty in unique_countries:
        mask = [curr == cty for curr in countries]
        color = COUNTRY_PALETTE.get(cty, "#80D8FF" if hash(cty) % 2 == 0 else "#FFD180")
        ax2.scatter(
            embedding[mask, 0], embedding[mask, 1],
            c=color, label=f"{cty} ({sum(mask)})", s=28, alpha=0.85, edgecolors="none"
        )
    ax2.legend(facecolor="#21262D", edgecolor="#30363D", labelcolor="#F0F6FC", fontsize=9, loc="upper right")
    ax2.tick_params(colors="#8B949E")
    ax2.grid(True, color="#30363D", linestyle="--", linewidth=0.5, alpha=0.5)

    plt.tight_layout()

    # Save to artifacts directory
    output_png = os.path.join(app_dir, "umap_comparative_genres_vs_countries.png")
    plt.savefig(output_png, dpi=200, bbox_inches="tight")
    plt.close()

    print(f"[UMAP Test] Successfully generated enriched UMAP comparison plot: {output_png}")


if __name__ == "__main__":
    run_comparative_umap()
