from __future__ import annotations
# pyrefly: ignore [missing-import]
import aiosqlite
import os
import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)


class ClosestMatchList(list):
    pass


class DatabaseManager:
    """
    Manages the music catalogue SQLite database with zero resource leaks.
    Concurrency: Shared persistent connection with a write-lock for mutations and lock-free reads.
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self._write_lock = asyncio.Lock()
        # Caches to speed up indexing: (name) -> id or (artist_id, title) -> id
        self._artist_cache: dict[str, int] = {}
        self._album_cache: dict[tuple[int, str], int] = {}
        # Load-once cache for the persisted NPMI genre-similarity model so the
        # walk reads it from memory after a single DB hit (rebuilt at graph gen).
        self._genre_affinity_cache: dict | None = None
        # Load-once cache for the persisted genre-adjacency graph (journey walk).
        self._genre_graph_cache: dict | None = None

    def clear_caches(self):
        """Clears the in-memory metadata caches. Call when DB state changes significantly."""
        self._artist_cache.clear()
        self._album_cache.clear()
        logger.debug("Database caches cleared.")

    async def get_connection(self):
        """
        Returns the shared persistent connection object.
        Initializes it if it doesn't exist.
        """
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path, timeout=30.0)
            self._conn.row_factory = aiosqlite.Row
            try:
                await self._conn.execute("PRAGMA journal_mode = WAL")
                await self._conn.execute("PRAGMA synchronous = NORMAL")
            except:
                await self._conn.execute("PRAGMA journal_mode = DELETE")
            await self._conn.execute("PRAGMA foreign_keys = ON")
            await self._conn.execute("PRAGMA cache_size = -65536")  # 64 MB page cache
            # Run playlist schema migration every time we open a fresh connection.
            # CREATE TABLE IF NOT EXISTS is idempotent; safe on existing databases.
            await self._migrate_playlists(self._conn)
            await self._migrate_partitions(self._conn)
            await self._migrate_pca(self._conn)
            await self._migrate_clusters(self._conn)
            await self._migrate_enrichment(self._conn)
            await self._migrate_album_genre_bucket(self._conn)
            await self._migrate_features_v4_to_v5(self._conn)
        return self._conn

    async def get_total_tracks(self) -> int:
        """Lock-free read. Returns total number of tracks in the database."""
        conn = await self.get_connection()
        async with conn.execute("SELECT COUNT(*) FROM tracks") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def close(self):
        """Explicitly closes the shared connection to prevent resource leaks."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed.")

    async def checkpoint(self):
        """Mutation: Forces a WAL checkpoint."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                logger.info("Database WAL checkpoint successful.")
            except Exception as e:
                logger.error(f"WAL checkpoint failed: {e}")

    async def initialize(self):
        """Mutation: Initializes the database schema from scratch."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                # Tables
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS artists (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        name        TEXT UNIQUE COLLATE NOCASE NOT NULL,
                        album_count INTEGER DEFAULT 0,
                        track_count INTEGER DEFAULT 0
                    )
                ''')

                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS albums (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        artist_id    INTEGER NOT NULL,
                        title        TEXT    NOT NULL COLLATE NOCASE,
                        year         TEXT,
                        genre        TEXT,
                        genre_bucket TEXT,
                        track_count  INTEGER DEFAULT 0,
                        FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE,
                        UNIQUE (artist_id, title)
                    )
                ''')

                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS tracks (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        album_id   INTEGER NOT NULL,
                        title      TEXT,
                        track_num  INTEGER,
                        duration   REAL,
                        path       TEXT UNIQUE NOT NULL,
                        format     TEXT,
                        added_date REAL,
                        bitrate    INTEGER,
                        bpm        REAL DEFAULT 0,
                        energy     REAL DEFAULT 0,
                        brightness REAL DEFAULT 0,
                        FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE
                    )
                ''')

                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS play_counts (
                        track_path  TEXT PRIMARY KEY,
                        count       INTEGER DEFAULT 0,
                        last_played INTEGER,
                        bpm         REAL DEFAULT 0,
                        energy      REAL DEFAULT 0,
                        brightness  REAL DEFAULT 0,
                        pca_coords  BLOB
                    )
                ''')

                # Indexes
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_artist_id  ON albums(artist_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_album_id   ON tracks(album_id)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_added_date ON tracks(added_date)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_title      ON tracks(title COLLATE NOCASE)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_title      ON albums(title COLLATE NOCASE)")

                await self._create_fts_and_triggers(conn)

                # Migration for KNN auto-playlist DSP feature columns
                for table in ["tracks", "play_counts"]:
                    for col in ["bpm", "energy", "brightness"]:
                        try:
                            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL DEFAULT 0")
                        except:
                            pass # Column already exists
                # Sound-profile vector: float32 MFCC mean + delta + chroma
                # packed as a single BLOB (v3 layout, see utils/dsp.py).
                # `features_version` invalidates cached values when the
                # extractor's output semantics change; older versions are
                # simply ignored at read time. Other columns are scalar
                # descriptors stored alongside bpm/energy/brightness for
                # cheap WHERE filters and direct mood-profile scoring.
                #
                # v3 additions: spectral_flatness (tonal vs noisy),
                # spectral_contrast (peak-to-valley dynamic range), and
                # key_index (Krumhansl-Schmuckler key estimate, 0-23).
                for col, ddl in [
                    ("timbre", "BLOB"),
                    ("features_version", "INTEGER DEFAULT 0"),
                    ("rolloff", "REAL DEFAULT 0"),
                    ("beat_strength", "REAL DEFAULT 0"),
                    ("spectral_flatness", "REAL DEFAULT 0"),
                    ("spectral_contrast", "REAL DEFAULT 0"),
                    ("key_index", "INTEGER DEFAULT 0"),
                ]:
                    try:
                        await conn.execute(
                            f"ALTER TABLE play_counts ADD COLUMN {col} {ddl}"
                        )
                    except:
                        pass

                # Track neighbour graph: sparse adjacency for k-NN traversal.
                # edge_kind ∈ {'acoustic', 'artist', 'album'}. Paths used as
                # keys to match the rest of the codebase (play_counts,
                # playlist_tracks). weight is cosine similarity for acoustic
                # edges, fixed 1.0 for metadata edges.
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS track_neighbors (
                        track_path    TEXT NOT NULL,
                        neighbor_path TEXT NOT NULL,
                        weight        REAL NOT NULL,
                        edge_kind     TEXT NOT NULL,
                        PRIMARY KEY (track_path, neighbor_path, edge_kind)
                    )
                ''')
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_neighbors_track "
                    "ON track_neighbors(track_path, edge_kind, weight DESC)"
                )

                # Persistent playback history. Drives two things:
                #   1) Long-term avoid set for the assistant's similarity walk
                #      (in-memory _recent loses everything on app restart).
                #   2) Listen-signal feedback that re-ranks mood candidates
                #      (skipped-early tracks sink, completed ones float up).
                # event ∈ {'played', 'skipped_early', 'completed'}. seed_path
                # is set when the track was reached via "play similar" so we
                # can later attribute skips/completions back to the seed for
                # online edge tuning if we ever want it.
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS playback_history (
                        track_path TEXT NOT NULL,
                        played_at  REAL NOT NULL,
                        event      TEXT NOT NULL,
                        seed_path  TEXT
                    )
                ''')
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_history_recent "
                    "ON playback_history(played_at DESC)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_history_track "
                    "ON playback_history(track_path, event)"
                )

                # Track partitions cache table (mood subsets + acoustic islets)
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS track_partitions (
                        track_path TEXT PRIMARY KEY REFERENCES tracks(path) ON DELETE CASCADE,
                        mood       TEXT,
                        islet_id   INTEGER
                    )
                ''')
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_tp_mood ON track_partitions(mood)")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_tp_islet_id ON track_partitions(islet_id)")

                # Mood feedback and profile adaptation tables
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS mood_feedback (
                        track_path TEXT,
                        mood       TEXT,
                        feedback   INTEGER,
                        PRIMARY KEY (track_path, mood)
                    )
                ''')
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS mood_profiles (
                        mood    TEXT,
                        feature TEXT,
                        target  REAL,
                        weight  REAL DEFAULT 1.0,
                        PRIMARY KEY (mood, feature)
                    )
                ''')
                # Migration for pre-v2 mood_profiles rows (target only).
                try:
                    await conn.execute(
                        "ALTER TABLE mood_profiles ADD COLUMN weight REAL DEFAULT 1.0"
                    )
                except Exception:
                    pass  # column already exists

                # Note: legacy `mood_regressors` table is no longer created on
                # new DBs (islet membership now uses cosine + JSON blacklist).
                # Existing user DBs may still carry the table; it is left
                # in place harmlessly rather than dropped destructively.

                await conn.commit()
            except Exception as exc:
                await conn.rollback()
                logger.exception("Initialization failed: %s", exc)
                raise
            finally:
                self.clear_caches()

    async def _create_fts_and_triggers(self, conn):
        """Internal helper: assumes connection is active."""
        triggers = [
            "trg_tracks_insert_counts", "trg_tracks_delete_counts", "trg_tracks_update_counts",
            "trg_tracks_update_fts", "trg_albums_insert_counts", "trg_albums_delete_counts",
            "trg_albums_update_fts", "trg_albums_artist_update_fts", "trg_albums_artist_update_counts",
            "trg_artists_update_fts"
        ]
        for t in triggers:
            await conn.execute(f"DROP TRIGGER IF EXISTS {t}")

        await conn.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_search USING fts5(
                title, album, artist, tokenize='unicode61'
            )
        ''')

        # Triggers
        await conn.execute('''
            CREATE TRIGGER trg_tracks_insert_counts AFTER INSERT ON tracks
            BEGIN
                UPDATE albums SET track_count = track_count + 1 WHERE id = NEW.album_id;
                UPDATE artists SET track_count = track_count + 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = NEW.album_id
                );
                INSERT INTO fts_search(rowid, title, album, artist)
                SELECT NEW.id, NEW.title, al.title, ar.name
                FROM albums al JOIN artists ar ON al.artist_id = ar.id WHERE al.id = NEW.album_id;
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_tracks_delete_counts AFTER DELETE ON tracks
            BEGIN
                UPDATE albums SET track_count = track_count - 1 WHERE id = OLD.album_id;
                UPDATE artists SET track_count = track_count - 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = OLD.album_id
                );
                DELETE FROM fts_search WHERE rowid = OLD.id;
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_tracks_update_counts AFTER UPDATE OF album_id ON tracks
            BEGIN
                UPDATE albums SET track_count = track_count - 1 WHERE id = OLD.album_id;
                UPDATE artists SET track_count = track_count - 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = OLD.album_id
                );
                UPDATE albums SET track_count = track_count + 1 WHERE id = NEW.album_id;
                UPDATE artists SET track_count = track_count + 1 WHERE id = (
                    SELECT artist_id FROM albums WHERE id = NEW.album_id
                );
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_tracks_update_fts AFTER UPDATE ON tracks
            BEGIN
                UPDATE fts_search
                SET title = NEW.title,
                    album = (SELECT title FROM albums WHERE id = NEW.album_id),
                    artist = (
                        SELECT ar.name FROM artists ar 
                        JOIN albums al ON al.artist_id = ar.id 
                        WHERE al.id = NEW.album_id
                    )
                WHERE rowid = NEW.id;
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_albums_insert_counts AFTER INSERT ON albums
            BEGIN
                UPDATE artists SET album_count = album_count + 1 WHERE id = NEW.artist_id;
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_albums_delete_counts AFTER DELETE ON albums
            BEGIN
                UPDATE artists SET album_count = album_count - 1 WHERE id = OLD.artist_id;
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_albums_update_fts AFTER UPDATE OF title ON albums
            BEGIN
                UPDATE fts_search SET album = NEW.title WHERE rowid IN (
                    SELECT id FROM tracks WHERE album_id = NEW.id
                );
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_albums_artist_update_fts AFTER UPDATE OF artist_id ON albums
            BEGIN
                UPDATE fts_search 
                SET artist = (SELECT name FROM artists WHERE id = NEW.artist_id)
                WHERE rowid IN (SELECT id FROM tracks WHERE album_id = NEW.id);
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_albums_artist_update_counts AFTER UPDATE OF artist_id ON albums
            BEGIN
                UPDATE artists SET 
                    album_count = album_count - 1,
                    track_count = track_count - (SELECT COUNT(*) FROM tracks WHERE album_id = OLD.id)
                WHERE id = OLD.artist_id;
                UPDATE artists SET 
                    album_count = album_count + 1,
                    track_count = track_count + (SELECT COUNT(*) FROM tracks WHERE album_id = NEW.id)
                WHERE id = NEW.artist_id;
            END;
        ''')

        await conn.execute('''
            CREATE TRIGGER trg_artists_update_fts AFTER UPDATE OF name ON artists
            BEGIN
                UPDATE fts_search SET artist = NEW.name WHERE rowid IN (
                    SELECT t.id FROM tracks t 
                    JOIN albums al ON t.album_id = al.id 
                    WHERE al.artist_id = NEW.id
                );
            END;
        ''')

    async def _get_or_create_artist(self, conn, name):
        name = name.strip() if name else "Unknown Artist"
        if name in self._artist_cache:
            return self._artist_cache[name]
            
        await conn.execute("INSERT OR IGNORE INTO artists (name) VALUES (?)", (name,))
        async with conn.execute("SELECT id FROM artists WHERE name = ?", (name,)) as cursor:
            row = await cursor.fetchone()
            artist_id = row['id']
            self._artist_cache[name] = artist_id
            return artist_id

    async def _get_or_create_album(self, conn, artist_id, title, year=None, genre=None):
        title = title.strip() if title else "Unknown Album"
        cache_key = (artist_id, title)
        if cache_key in self._album_cache:
            return self._album_cache[cache_key]
            
        await conn.execute(
            "INSERT OR IGNORE INTO albums (artist_id, title, year, genre) VALUES (?, ?, ?, ?)",
            (artist_id, title, year, genre)
        )
        async with conn.execute(
            "SELECT id FROM albums WHERE artist_id = ? AND title = ?", (artist_id, title)
        ) as cursor:
            row = await cursor.fetchone()
            album_id = row['id']
            self._album_cache[cache_key] = album_id
            return album_id

    async def insert_track_hierarchical(self, track_data):
        """Mutation."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                artist_id = await self._get_or_create_artist(conn, track_data.get('artist'))
                album_id  = await self._get_or_create_album(
                    conn, artist_id, track_data.get('album'),
                    track_data.get('year'), track_data.get('genre')
                )
                await conn.execute(
                    '''INSERT OR REPLACE INTO tracks
                       (album_id, title, track_num, duration, path, format, added_date, bitrate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        album_id, track_data.get('title', 'Unknown'), track_data.get('track_num'),
                        track_data.get('duration', 0.0), track_data.get('path'),
                        track_data.get('format', ''), track_data.get('added_date', 0.0),
                        track_data.get('bitrate', 0)
                    )
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def begin_bulk_import(self):
        """
        Mutation: drops the per-row INSERT trigger for bulk imports.
        Eliminates the O(N) trigger overhead (2 UPDATEs + 1 FTS5 INSERT per row).
        Must be paired with end_bulk_import() to restore counts and FTS.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute("DROP TRIGGER IF EXISTS trg_tracks_insert_counts")
            await conn.commit()

    async def end_bulk_import(self):
        """
        Mutation: rebuilds track/album/artist counts and the FTS index in a single
        pass, then recreates the per-row INSERT trigger for ongoing maintenance.
        Call after begin_bulk_import() + all batch inserts are done.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("BEGIN")
                # Rebuild counts from scratch.
                await conn.execute(
                    "UPDATE albums SET track_count = "
                    "(SELECT COUNT(*) FROM tracks WHERE album_id = albums.id)"
                )
                await conn.execute(
                    "UPDATE artists SET track_count = "
                    "(SELECT COUNT(*) FROM tracks t "
                    " JOIN albums al ON t.album_id = al.id "
                    " WHERE al.artist_id = artists.id)"
                )
                await conn.execute(
                    "UPDATE artists SET album_count = "
                    "(SELECT COUNT(*) FROM albums WHERE artist_id = artists.id)"
                )
                # Rebuild FTS index in one bulk INSERT.
                await conn.execute("DELETE FROM fts_search")
                await conn.execute(
                    "INSERT INTO fts_search(rowid, title, album, artist) "
                    "SELECT t.id, t.title, al.title, ar.name "
                    "FROM tracks t "
                    "JOIN albums al ON al.id = t.album_id "
                    "JOIN artists ar ON ar.id = al.artist_id"
                )
                # Recreate the trigger for future single-track inserts.
                await conn.execute('''
                    CREATE TRIGGER trg_tracks_insert_counts AFTER INSERT ON tracks
                    BEGIN
                        UPDATE albums SET track_count = track_count + 1 WHERE id = NEW.album_id;
                        UPDATE artists SET track_count = track_count + 1 WHERE id = (
                            SELECT artist_id FROM albums WHERE id = NEW.album_id
                        );
                        INSERT INTO fts_search(rowid, title, album, artist)
                        SELECT NEW.id, NEW.title, al.title, ar.name
                        FROM albums al JOIN artists ar ON al.artist_id = ar.id WHERE al.id = NEW.album_id;
                    END;
                ''')
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def insert_tracks_batch(self, tracks_data_list):
        """Mutation: Transaction-safe batch insertion."""
        if not tracks_data_list: return
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("BEGIN")
                for track_data in tracks_data_list:
                    artist_id = await self._get_or_create_artist(conn, track_data.get('artist'))
                    album_id  = await self._get_or_create_album(
                        conn, artist_id, track_data.get('album'),
                        track_data.get('year'), track_data.get('genre')
                    )
                    await conn.execute(
                        '''INSERT OR REPLACE INTO tracks
                           (album_id, title, track_num, duration, path, format, added_date, bitrate)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (
                            album_id, track_data.get('title', 'Unknown'), track_data.get('track_num'),
                            track_data.get('duration', 0.0), track_data.get('path'),
                            track_data.get('format', ''), track_data.get('added_date', 0.0),
                            track_data.get('bitrate', 0)
                        )
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def update_track_metadata(self, track_path, tag_data):
        """Mutation."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                async with conn.execute('''
                    SELECT t.id, t.album_id, al.artist_id, al.title AS album_title, 
                           ar.name AS artist_name, al.year, al.genre
                    FROM tracks t JOIN albums al ON t.album_id = al.id JOIN artists ar ON al.artist_id = ar.id
                    WHERE t.path = ?
                ''', (track_path,)) as cursor:
                    row = await cursor.fetchone()
                    if not row: return

                new_artist_name = tag_data.get('artist', row['artist_name'])
                new_album_title = tag_data.get('album', row['album_title'])
                new_year = tag_data.get('year', row['year'])
                new_genre = tag_data.get('genre', row['genre'])

                if (new_artist_name != row['artist_name'] or new_album_title != row['album_title'] or 
                    new_year != row['year'] or new_genre != row['genre']):
                    
                    new_artist_id = await self._get_or_create_artist(conn, new_artist_name)
                    new_album_id = await self._get_or_create_album(
                        conn, new_artist_id, new_album_title, new_year, new_genre
                    )
                    await conn.execute("UPDATE tracks SET album_id = ? WHERE path = ?", (new_album_id, track_path))

                if 'title' in tag_data:
                    await conn.execute("UPDATE tracks SET title = ? WHERE path = ?", (tag_data['title'], track_path))
                if 'track_num' in tag_data:
                    await conn.execute("UPDATE tracks SET track_num = ? WHERE path = ?", (tag_data['track_num'], track_path))

                await conn.execute("DELETE FROM albums WHERE track_count <= 0")
                await conn.execute("DELETE FROM artists WHERE album_count <= 0")
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def delete_tracks_by_paths(self, paths):
        """Mutation."""
        if not paths: return
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                for i in range(0, len(paths), 900):
                    chunk = paths[i:i + 900]
                    placeholders = ",".join("?" * len(chunk))
                    # Drop the playlist memberships first so reordering a
                    # playlist after a deletion never collides with orphan
                    # order_index slots: the UI's visible list is joined to
                    # tracks, so orphans would otherwise stay in the DB
                    # invisibly and corrupt subsequent reorder writes.
                    await conn.execute(
                        f"DELETE FROM playlist_tracks WHERE track_path IN ({placeholders})",
                        chunk,
                    )
                    await conn.execute(
                        f"DELETE FROM tracks WHERE path IN ({placeholders})", chunk
                    )
                await conn.execute("DELETE FROM albums  WHERE track_count <= 0")
                await conn.execute("DELETE FROM artists WHERE album_count <= 0")
                await conn.commit()
                self.clear_caches()
            except Exception:
                await conn.rollback()
                raise

    async def prune_orphans(self):
        """Mutation."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("DELETE FROM albums  WHERE track_count <= 0")
                await conn.execute("DELETE FROM artists WHERE album_count <= 0")
                await conn.commit()
                self.clear_caches()
            except Exception:
                await conn.rollback()
                raise

    async def get_path_mtime_map(self):
        """Lock-free read."""
        conn = await self.get_connection()
        async with conn.execute("SELECT path, added_date FROM tracks") as cursor:
            rows = await cursor.fetchall()
            return {r['path']: r['added_date'] for r in rows}

    @staticmethod
    def _safe_fts(query: str) -> str:
        q = re.sub(r'[\"*\-^\'\[\]{}~:()|,]', ' ', query).strip()
        q = re.sub(r'\s+', ' ', q)
        return q

    @staticmethod
    def _kmer_similarity(query: str, target: str) -> float:
        if not query or not target:
            return 0.0
        q = query.lower().strip()
        t = target.lower().strip()
        if q == t:
            return 1.0
        if q in t:
            return 0.8 + 0.2 * (len(q) / len(t))
            
        q_set = {q[i:i+2] for i in range(len(q) - 1)}
        t_set = {t[i:i+2] for i in range(len(t) - 1)}
        
        if not q_set or not t_set:
            return 0.0
            
        intersection = q_set.intersection(t_set)
        return 2.0 * len(intersection) / (len(q_set) + len(t_set))

    async def get_all_tracks(self, search_query="", sort_mode="date"):
        """Lock-free read."""
        sort_map = {
            "date": "t.added_date DESC", "artist": "ar.name COLLATE NOCASE ASC",
            "album": "al.title COLLATE NOCASE ASC", "track": "t.title COLLATE NOCASE ASC",
        }
        order = sort_map.get(sort_mode, "t.added_date DESC")
        base = '''
            SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
            FROM tracks t JOIN albums al ON t.album_id = al.id JOIN artists ar ON al.artist_id = ar.id
        '''
        conn = await self.get_connection()
        if not search_query:
            async with conn.execute(f"{base} ORDER BY {order}") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

        like_q = f"%{search_query}%"
        safe_q = self._safe_fts(search_query)

        if safe_q:
            try:
                fts_sql = f"{base} JOIN fts_search f ON f.rowid = t.id WHERE fts_search MATCH ? ORDER BY {order}"
                async with conn.execute(fts_sql, [f"{safe_q}*"]) as cursor:
                    rows = await cursor.fetchall()
                    if rows: return [dict(r) for r in rows]
            except Exception: pass

        like_sql = f"{base} WHERE t.title LIKE ? OR al.title LIKE ? OR ar.name LIKE ? ORDER BY {order}"
        async with conn.execute(like_sql, [like_q, like_q, like_q]) as cursor:
            rows = await cursor.fetchall()
            results = [dict(r) for r in rows]

        if not results and search_query:
            # Fallback to closest match
            async with conn.execute(f"{base} ORDER BY {order}") as cursor:
                all_rows = await cursor.fetchall()
            scored_rows = []
            for r in all_rows:
                title = r["title"] or ""
                album = r["album"] or ""
                artist = r["artist"] or ""
                score = max(
                    self._kmer_similarity(search_query, title),
                    self._kmer_similarity(search_query, album),
                    self._kmer_similarity(search_query, artist)
                )
                if score >= 0.25:
                    scored_rows.append((score, dict(r)))
            scored_rows.sort(key=lambda x: x[0], reverse=True)
            results = ClosestMatchList(item for _, item in scored_rows)
            results.is_closest = True

        return results

    async def get_all_albums(self, search_query="", sort_mode="date"):
        """Lock-free read."""
        sort_map = {
            "date": "latest_added DESC", 
            "artist": "ar.name COLLATE NOCASE ASC", 
            "album": "al.title COLLATE NOCASE ASC",
            "track": "al.title COLLATE NOCASE ASC"
        }
        order = sort_map.get(sort_mode, "latest_added DESC")
        base = '''
            SELECT al.id, al.title AS album, al.year, al.genre, ar.name AS artist, al.track_count,
                   (SELECT MAX(added_date) FROM tracks WHERE album_id = al.id) AS latest_added
            FROM albums al JOIN artists ar ON al.artist_id = ar.id
        '''
        conn = await self.get_connection()
        if not search_query:
            async with conn.execute(f"{base} ORDER BY {order}") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

        like_q = f"%{search_query}%"
        safe_q = self._safe_fts(search_query)
        fts_subq, params = "", [like_q, like_q]

        if safe_q:
            try:
                async with conn.execute("SELECT rowid FROM fts_search WHERE fts_search MATCH ? LIMIT 1", [f"{safe_q}*"]) as cursor:
                    if await cursor.fetchone():
                        fts_subq = "OR al.id IN (SELECT DISTINCT album_id FROM tracks WHERE id IN (SELECT rowid FROM fts_search WHERE fts_search MATCH ?))"
                        params.append(f"{safe_q}*")
            except Exception: pass

        sql = f"{base} WHERE al.title LIKE ? OR ar.name LIKE ? {fts_subq} ORDER BY {order}"
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            results = [dict(r) for r in rows]

        if not results and search_query:
            # Fallback to closest match
            async with conn.execute(f"{base} ORDER BY {order}") as cursor:
                all_rows = await cursor.fetchall()
            scored_rows = []
            for r in all_rows:
                album = r["album"] or ""
                artist = r["artist"] or ""
                score = max(
                    self._kmer_similarity(search_query, album),
                    self._kmer_similarity(search_query, artist)
                )
                if score >= 0.25:
                    scored_rows.append((score, dict(r)))
            scored_rows.sort(key=lambda x: x[0], reverse=True)
            results = ClosestMatchList(item for _, item in scored_rows)
            results.is_closest = True

        return results

    async def get_all_artists(self, search_query="", sort_mode="name"):
        """Lock-free read."""
        sort_map = {
            "name": "name COLLATE NOCASE ASC",
            "artist": "name COLLATE NOCASE ASC",
            "tracks": "track_count DESC",
            "albums": "album_count DESC",
            "track": "name COLLATE NOCASE ASC"
        }
        order = sort_map.get(sort_mode, "name COLLATE NOCASE ASC")
        base = "SELECT id, name, album_count, track_count FROM artists"
        conn = await self.get_connection()
        if not search_query:
            async with conn.execute(f"{base} ORDER BY {order}") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

        like_q = f"%{search_query}%"
        safe_q = self._safe_fts(search_query)
        fts_subq, params = "", [like_q]

        if safe_q:
            try:
                async with conn.execute("SELECT rowid FROM fts_search WHERE fts_search MATCH ? LIMIT 1", [f"{safe_q}*"]) as cursor:
                    if await cursor.fetchone():
                        fts_subq = "OR id IN (SELECT DISTINCT ar2.id FROM artists ar2 JOIN albums al2 ON al2.artist_id = ar2.id JOIN tracks t2 ON t2.album_id = al2.id WHERE t2.id IN (SELECT rowid FROM fts_search WHERE fts_search MATCH ?))"
                        params.append(f"{safe_q}*")
            except Exception: pass

        sql = f"{base} WHERE name LIKE ? {fts_subq} ORDER BY {order}"
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            results = [dict(r) for r in rows]

        if not results and search_query:
            # Fallback to closest match
            async with conn.execute(f"{base} ORDER BY {order}") as cursor:
                all_rows = await cursor.fetchall()
            scored_rows = []
            for r in all_rows:
                name = r["name"] or ""
                score = self._kmer_similarity(search_query, name)
                if score >= 0.25:
                    scored_rows.append((score, dict(r)))
            scored_rows.sort(key=lambda x: x[0], reverse=True)
            results = ClosestMatchList(item for _, item in scored_rows)
            results.is_closest = True

        return results

    async def get_tracks_by_album(self, album_title, artist_name):
        """Lock-free read."""
        sql = '''
            SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
            FROM tracks t JOIN albums al ON t.album_id = al.id JOIN artists ar ON al.artist_id = ar.id
            WHERE al.title = ? AND ar.name = ? ORDER BY t.track_num ASC
        '''
        conn = await self.get_connection()
        async with conn.execute(sql, (album_title, artist_name)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_albums_by_artist(self, artist_name):
        """Lock-free read."""
        sql = '''
            SELECT al.id, al.title AS album, al.year, al.genre, ar.name AS artist, al.track_count,
                   (SELECT MAX(added_date) FROM tracks WHERE album_id = al.id) AS latest_added
            FROM albums al JOIN artists ar ON al.artist_id = ar.id
            WHERE ar.name = ? ORDER BY al.title ASC
        '''
        conn = await self.get_connection()
        async with conn.execute(sql, (artist_name,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_track_by_meta(self, title, artist):
        """Lock-free read: Returns a single track if title and artist match."""
        sql = '''
            SELECT t.id FROM tracks t 
            JOIN albums al ON t.album_id = al.id 
            JOIN artists ar ON al.artist_id = ar.id
            WHERE t.title = ? COLLATE NOCASE AND ar.name = ? COLLATE NOCASE
            LIMIT 1
        '''
        conn = await self.get_connection()
        async with conn.execute(sql, (title, artist)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_album_by_meta(self, title, artist):
        """Lock-free read: Returns a single album if title and artist match."""
        sql = '''
            SELECT al.id FROM albums al 
            JOIN artists ar ON al.artist_id = ar.id
            WHERE al.title = ? COLLATE NOCASE AND ar.name = ? COLLATE NOCASE
            LIMIT 1
        '''
        conn = await self.get_connection()
        async with conn.execute(sql, (title, artist)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ─── Playlist Schema Migration ─────────────────────────────────────────────

    async def _migrate_playlists(self, conn):
        """
        Idempotent migration: creates the playlists and playlist_tracks tables
        if they don't already exist. Called once per fresh connection so it
        works transparently on existing databases without a version counter.

        Schema:
          playlists(id, name UNIQUE, created)
          playlist_tracks(id, playlist_id FK, track_path, order_index)

        order_index preserves insertion order and leaves room for future
        drag-to-reorder without a schema change.
        """
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS playlists (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created INTEGER DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                color   TEXT
            )
        ''')
        # Idempotent migration for existing tables
        try:
            await conn.execute("ALTER TABLE playlists ADD COLUMN color TEXT")
            await conn.commit()
        except:
            pass
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS playlist_tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL
                                REFERENCES playlists(id) ON DELETE CASCADE,
                track_path  TEXT    NOT NULL,
                order_index INTEGER NOT NULL DEFAULT 0,
                UNIQUE (playlist_id, track_path)
            )
        ''')
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pt_playlist_id "
            "ON playlist_tracks(playlist_id, order_index)"
        )
        await conn.commit()

    # ─── Partition Schema Migration & Helpers ─────────────────────────────────

    async def _migrate_partitions(self, conn):
        """
        Idempotent migration: creates the track_partitions table and its indexes
        if they don't already exist. Called once per fresh connection.
        """
        # Composite PK (track_path, mood) → a track may be pinned to several
        # moods (many-to-many, revised 2026-06-04).
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS track_partitions (
                track_path TEXT REFERENCES tracks(path) ON DELETE CASCADE,
                mood       TEXT,
                islet_id   INTEGER,
                PRIMARY KEY (track_path, mood)
            )
        ''')
        # Migrate a legacy single-column-PK table (strict one-to-many) to the
        # composite PK. SQLite can't alter a PK in place, so rebuild once.
        try:
            cur = await conn.execute("PRAGMA table_info(track_partitions)")
            info = await cur.fetchall()
            pk_cols = [c["name"] for c in info if c["pk"]]
            if pk_cols == ["track_path"]:
                await conn.execute("ALTER TABLE track_partitions RENAME TO track_partitions_legacy")
                await conn.execute('''
                    CREATE TABLE track_partitions (
                        track_path TEXT REFERENCES tracks(path) ON DELETE CASCADE,
                        mood       TEXT,
                        islet_id   INTEGER,
                        PRIMARY KEY (track_path, mood)
                    )
                ''')
                await conn.execute(
                    "INSERT OR IGNORE INTO track_partitions (track_path, mood, islet_id) "
                    "SELECT track_path, mood, islet_id FROM track_partitions_legacy WHERE mood IS NOT NULL"
                )
                await conn.execute("DROP TABLE track_partitions_legacy")
                await conn.commit()
                logger.info("Migrated track_partitions to composite (track_path, mood) PK.")
        except Exception as e:
            logger.warning("track_partitions PK migration skipped: %s", e)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tp_mood ON track_partitions(mood)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tp_islet_id ON track_partitions(islet_id)")

        # Create mood_feedback and mood_profiles tables
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS mood_feedback (
                track_path TEXT,
                mood       TEXT,
                feedback   INTEGER,
                PRIMARY KEY (track_path, mood)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS mood_profiles (
                mood    TEXT,
                feature TEXT,
                target  REAL,
                PRIMARY KEY (mood, feature)
            )
        ''')
        await conn.commit()

    # ─── Playlist CRUD ─────────────────────────────────────────────────────────

    async def get_all_playlists(self, search_query: str = "", sort_mode: str = "date") -> list[dict]:
        """Lock-free read. Returns all playlists sorted by name or creation date."""
        order_col = "p.name COLLATE NOCASE ASC" if sort_mode == "name" else "p.created DESC"
        conn = await self.get_connection()
        
        sql = f'''
            SELECT p.id, p.name, p.created, p.color, COUNT(pt.track_path) AS track_count
            FROM playlists p
            LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
            {"WHERE p.name LIKE ?" if search_query else ""}
            GROUP BY p.id
            ORDER BY {order_col}
        '''
        
        params = (f"%{search_query}%",) if search_query else ()
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            results = [dict(r) for r in rows]

        if not results and search_query:
            sql_all = f'''
                SELECT p.id, p.name, p.created, p.color, COUNT(pt.track_path) AS track_count
                FROM playlists p
                LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id
                GROUP BY p.id
                ORDER BY {order_col}
            '''
            async with conn.execute(sql_all) as cursor:
                all_rows = await cursor.fetchall()
            scored_rows = []
            for r in all_rows:
                name = r["name"] or ""
                score = self._kmer_similarity(search_query, name)
                if score >= 0.25:
                    scored_rows.append((score, dict(r)))
            scored_rows.sort(key=lambda x: x[0], reverse=True)
            results = ClosestMatchList(item for _, item in scored_rows)
            results.is_closest = True

        return results

    async def get_tracks_in_playlist(self, playlist_id: int) -> list[dict]:
        """
        Lock-free read. Returns full track metadata for every track in the
        playlist, ordered by order_index (insertion order).
        """
        sql = '''
            SELECT t.*, al.title AS album, ar.name AS artist, al.year, al.genre
            FROM playlist_tracks pt
            JOIN tracks t  ON t.path    = pt.track_path
            JOIN albums al ON al.id     = t.album_id
            JOIN artists ar ON ar.id    = al.artist_id
            WHERE pt.playlist_id = ?
            ORDER BY pt.order_index ASC
        '''
        conn = await self.get_connection()
        async with conn.execute(sql, (playlist_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def update_playlist(self, playlist_id, name=None, color=None):
        """Mutation."""
        async with self._write_lock:
            conn = await self.get_connection()
            if name:
                await conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))
            if color:
                await conn.execute("UPDATE playlists SET color = ? WHERE id = ?", (color, playlist_id))
            await conn.commit()

    async def move_playlist_track(self, playlist_id, from_idx, to_idx):
        """Mutation: reorder a track within a playlist.

        `from_idx`/`to_idx` are positions in the *visible* playlist (the list
        the user sees in the UI). The visible list is the result of an inner
        join with `tracks`, so any playlist_tracks row whose underlying track
        has been deleted is hidden; but its `order_index` is still occupying
        a slot in the DB. Without cleanup the rewritten indices would collide
        with these orphan rows and the next reorder would behave erratically.

        We GC the orphan rows first, then operate on the surviving rows. This
        keeps "I deleted some songs" from breaking subsequent reorders.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                "DELETE FROM playlist_tracks "
                "WHERE playlist_id = ? "
                "AND track_path NOT IN (SELECT path FROM tracks)",
                (playlist_id,),
            )

            async with conn.execute(
                "SELECT track_path FROM playlist_tracks "
                "WHERE playlist_id = ? ORDER BY order_index ASC",
                (playlist_id,),
            ) as cursor:
                rows = await cursor.fetchall()
            paths = [r["track_path"] for r in rows]
            if not paths:
                await conn.commit()
                return

            # Clamp the indices defensively; UI events can race against a
            # concurrent removal and arrive with stale positions.
            from_idx = max(0, min(len(paths) - 1, int(from_idx)))
            to_idx = max(0, min(len(paths), int(to_idx)))
            item = paths.pop(from_idx)
            paths.insert(to_idx, item)

            for i, p in enumerate(paths):
                await conn.execute(
                    "UPDATE playlist_tracks SET order_index = ? "
                    "WHERE playlist_id = ? AND track_path = ?",
                    (i, playlist_id, p),
                )
            await conn.commit()

    async def remove_track_from_playlist(self, playlist_id, track_path):
        """Mutation."""
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_path = ?",
                (playlist_id, track_path)
            )
            await conn.commit()

    async def create_playlist(self, name: str) -> int:
        """
        Mutation. Creates a new playlist and returns its id.
        Raises sqlite3.IntegrityError if the name already exists.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                cursor = await conn.execute(
                    "INSERT INTO playlists (name) VALUES (?)", (name.strip(),)
                )
                await conn.commit()
                return cursor.lastrowid
            except Exception:
                await conn.rollback()
                raise

    async def add_track_to_playlist(self, playlist_id: int, track_path: str):
        """
        Mutation. Appends track_path to the playlist at the next order_index.
        UNIQUE(playlist_id, track_path) silently prevents duplicates.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                # Compute next order_index atomically inside the transaction.
                async with conn.execute(
                    "SELECT COALESCE(MAX(order_index), -1) + 1 AS next_idx "
                    "FROM playlist_tracks WHERE playlist_id = ?",
                    (playlist_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    next_idx = row["next_idx"] if row else 0

                await conn.execute(
                    "INSERT OR IGNORE INTO playlist_tracks "
                    "(playlist_id, track_path, order_index) VALUES (?, ?, ?)",
                    (playlist_id, track_path, next_idx),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def remove_track_from_playlist(self, playlist_id: int, track_path: str):
        """Mutation. Removes a single track from a playlist by path."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_path = ?",
                    (playlist_id, track_path),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def delete_playlist(self, playlist_id: int):
        """
        Mutation. Deletes the playlist and all its tracks.
        ON DELETE CASCADE in playlist_tracks handles the child rows.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute(
                    "DELETE FROM playlists WHERE id = ?", (playlist_id,)
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def rename_playlist(self, playlist_id: int, new_name: str):
        """Mutation. Renames a playlist. Raises IntegrityError on name clash."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute(
                    "UPDATE playlists SET name = ? WHERE id = ?",
                    (new_name.strip(), playlist_id),
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            
    async def increment_play_count(self, path: str):
        """Increments play count. Also stores/updates features for AutoPlaylist usage."""
        async with self._write_lock:
            conn = await self.get_connection()
            
            # Fetch features from tracks table first to propagate them to play_counts
            sql_feat = "SELECT bpm, energy, brightness FROM tracks WHERE path = ?"
            bpm, energy, brightness = 0.0, 0.0, 0.0
            async with conn.execute(sql_feat, (path,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    bpm, energy, brightness = row

            await conn.execute('''
                INSERT INTO play_counts (track_path, count, last_played, bpm, energy, brightness) 
                VALUES (?, 1, strftime('%s','now'), ?, ?, ?)
                ON CONFLICT(track_path) DO UPDATE SET 
                    count = count + 1,
                    last_played = strftime('%s','now'),
                    bpm = EXCLUDED.bpm,
                    energy = EXCLUDED.energy,
                    brightness = EXCLUDED.brightness
            ''', (path, bpm, energy, brightness))
            await conn.commit()

    async def get_most_played(self, limit=5) -> list[dict]:
        """Joins play_counts with tracks to get metadata for the landing page."""
        conn = await self.get_connection()
        sql = f'''
        SELECT t.*, al.title AS album, ar.name AS artist, pc.count
        FROM play_counts pc
        JOIN tracks t ON t.path = pc.track_path
        JOIN albums al ON t.album_id = al.id
        JOIN artists ar ON al.artist_id = ar.id
        ORDER BY pc.count DESC, pc.last_played DESC
        LIMIT ?
    '''
        async with conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_autoplaylist_hot_set(self) -> list[dict]:
        """
        Retrieves the 'Hot Set' for the KNN auto-playlist selector.
        Filters tracks based on play count quartiles.
        Rule: count >= 1/2 of Q1 (lowest quartile).

        Returns all hot-set entries regardless of whether their DSP features
        are populated; the caller is expected to invoke the DSP analyser
        for any entry whose `features_version` is missing or stale and then
        persist via `update_track_features`.
        """
        import numpy as np
        conn = await self.get_connection()

        # 1. Fetch all counts to calculate Q1
        async with conn.execute("SELECT count FROM play_counts") as cursor:
            rows = await cursor.fetchall()
            if not rows:
                return []
            counts = [r[0] for r in rows]

        # 2. Calculate Q1 (25th percentile)
        q1 = np.percentile(counts, 25)
        threshold = q1 / 2.0

        # 3. Fetch tracks that meet the 'Hot Set' threshold, including
        #    artist and album names for the string-similarity blending step
        #    in the auto-playlist KNN selector. LEFT JOINs are used so a
        #    missing tracks row (shouldn't happen) still returns the play_count
        #    entry with NULL artist/album rather than silently dropping it.
        sql = '''
            SELECT pc.track_path AS path,
                   pc.bpm, pc.energy, pc.brightness,
                   COALESCE(pc.rolloff, 0)         AS rolloff,
                   COALESCE(pc.beat_strength, 0)   AS beat_strength,
                   COALESCE(pc.spectral_flatness, 0) AS spectral_flatness,
                   COALESCE(pc.spectral_contrast, 0) AS spectral_contrast,
                   COALESCE(pc.key_index, 0)         AS key_index,
                   pc.timbre,
                   COALESCE(pc.features_version, 0) AS features_version,
                   pc.count,
                   ar.name  AS artist,
                   al.title AS album
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path       = pc.track_path
            LEFT JOIN albums  al ON al.id         = t.album_id
            LEFT JOIN artists ar ON ar.id         = al.artist_id
            WHERE pc.count >= ?
            ORDER BY pc.count DESC
        '''
        async with conn.execute(sql, (threshold,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


    async def update_track_features(
        self,
        path: str,
        bpm: float,
        energy: float,
        brightness: float,
        rolloff: float,
        beat_strength: float,
        spectral_flatness: float,
        spectral_contrast: float,
        key_index: int,
        timbre_blob: bytes,
        features_version: int,
    ):
        """Persist v3 DSP features. Upserts on track_path so the row exists
        even if increment_play_count hasn't fired yet."""
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                '''
                INSERT INTO play_counts
                    (track_path, count, last_played, bpm, energy, brightness,
                     rolloff, beat_strength, spectral_flatness,
                     spectral_contrast, key_index, timbre, features_version)
                VALUES (?, 0, strftime('%s','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(track_path) DO UPDATE SET
                    bpm = EXCLUDED.bpm,
                    energy = EXCLUDED.energy,
                    brightness = EXCLUDED.brightness,
                    rolloff = EXCLUDED.rolloff,
                    beat_strength = EXCLUDED.beat_strength,
                    spectral_flatness = EXCLUDED.spectral_flatness,
                    spectral_contrast = EXCLUDED.spectral_contrast,
                    key_index = EXCLUDED.key_index,
                    timbre = EXCLUDED.timbre,
                    features_version = EXCLUDED.features_version
                ''',
                (path, bpm, energy, brightness, rolloff, beat_strength,
                 spectral_flatness, spectral_contrast, key_index,
                 timbre_blob, features_version),
            )
            await conn.commit()

    # ── Track Graph (k-NN neighbours) ────────────────────────────────────────

    async def get_tracks_with_features(self, features_version: int) -> list[dict]:
        """Returns every track whose DSP features are present and current,
        joined with the metadata fields the assistant needs to enqueue them
        (title/artist/album/duration). Used by the graph builder for
        acoustic edges AND by the mood-based DSP search."""
        conn = await self.get_connection()
        sql = '''
            SELECT pc.track_path AS path, pc.timbre,
                   COALESCE(pc.bpm, 0)           AS bpm,
                   COALESCE(pc.brightness, 0)    AS brightness,
                   COALESCE(pc.energy, 0)        AS energy,
                   COALESCE(pc.rolloff, 0)       AS rolloff,
                   COALESCE(pc.beat_strength, 0) AS beat_strength,
                   COALESCE(pc.spectral_flatness, 0) AS spectral_flatness,
                   COALESCE(pc.spectral_contrast, 0) AS spectral_contrast,
                   COALESCE(pc.key_index, 0)         AS key_index,
                   t.title, t.duration,
                   ar.name  AS artist,
                   al.title AS album,
                   al.genre AS genre
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path  = pc.track_path
            LEFT JOIN albums  al ON al.id   = t.album_id
            LEFT JOIN artists ar ON ar.id   = al.artist_id
            WHERE pc.timbre IS NOT NULL
              AND COALESCE(pc.features_version, 0) >= ?
        '''
        async with conn.execute(sql, (features_version,)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_tracks_missing_features(self, features_version: int) -> list[str]:
        """Returns paths whose DSP features are absent or stale — the work
        queue for the bulk analyser sweep."""
        conn = await self.get_connection()
        sql = '''
            SELECT t.path
            FROM tracks t
            LEFT JOIN play_counts pc ON pc.track_path = t.path
            WHERE pc.timbre IS NULL
               OR COALESCE(pc.features_version, 0) < ?
        '''
        async with conn.execute(sql, (features_version,)) as cursor:
            return [r[0] for r in await cursor.fetchall()]

    async def replace_neighbors_bulk(self, edges: list[tuple], edge_kind: str):
        """Mutation: Atomically replaces every edge of `edge_kind` with the
        provided list. `edges` is a list of (track_path, neighbor_path, weight)
        tuples. Used to rebuild a whole tier of the graph in one transaction.
        Self-loops are silently dropped."""
        clean = [(t, n, float(w)) for (t, n, w) in edges if t != n]
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                "DELETE FROM track_neighbors WHERE edge_kind = ?", (edge_kind,)
            )
            if clean:
                await conn.executemany(
                    "INSERT OR REPLACE INTO track_neighbors "
                    "(track_path, neighbor_path, weight, edge_kind) "
                    "VALUES (?, ?, ?, ?)",
                    [(t, n, w, edge_kind) for (t, n, w) in clean],
                )
            await conn.commit()

    async def get_neighbors(
        self,
        track_path: str,
        k: int = 20,
        edge_kind: str | None = None,
    ) -> list[dict]:
        """Returns up to `k` neighbours of `track_path`, joined with track
        metadata. Pass edge_kind to restrict to one tier (acoustic / artist /
        album); None returns the highest-weighted edges across all tiers."""
        conn = await self.get_connection()
        if edge_kind:
            sql = '''
                SELECT n.neighbor_path AS path, n.weight, n.edge_kind,
                       t.title, ar.name AS artist, al.title AS album
                FROM track_neighbors n
                LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
                LEFT JOIN albums  al ON al.id      = t.album_id
                LEFT JOIN artists ar ON ar.id      = al.artist_id
                WHERE n.track_path = ? AND n.edge_kind = ?
                ORDER BY n.weight DESC
                LIMIT ?
            '''
            params: tuple = (track_path, edge_kind, k)
        else:
            sql = '''
                SELECT n.neighbor_path AS path, n.weight, n.edge_kind,
                       t.title, ar.name AS artist, al.title AS album
                FROM track_neighbors n
                LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
                LEFT JOIN albums  al ON al.id      = t.album_id
                LEFT JOIN artists ar ON ar.id      = al.artist_id
                WHERE n.track_path = ?
                ORDER BY n.weight DESC
                LIMIT ?
            '''
            params = (track_path, k)
        async with conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def count_tracks_with_coords(self) -> int:
        """Number of tracks carrying a persisted Zr coordinate — the REAL
        'is the similarity graph built?' predicate.

        The walk's similarity oracle is the coordinate graph
        (`track_graph.load_live_coordinate_graph`, which reads exactly this
        column), NOT the `track_neighbors` table. `build_acoustic_edges` stopped
        writing acoustic rows when the geometry moved to persisted Zr coords, so
        `count_neighbors(KIND_ACOUSTIC)` is now permanently 0 on a perfectly
        healthy library — callers that used it to decide whether to build were
        reading a table nothing writes any more."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT COUNT(*) FROM play_counts WHERE pca_coords IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def count_neighbors(self, edge_kind: str | None = None) -> int:
        """Total edge count, optionally filtered by kind.

        NB: this is NOT a graph-readiness check for the acoustic tier — see
        `count_tracks_with_coords`. Only the metadata tiers ('artist'/'album')
        still write rows here."""
        conn = await self.get_connection()
        if edge_kind:
            sql = "SELECT COUNT(*) FROM track_neighbors WHERE edge_kind = ?"
            params: tuple = (edge_kind,)
        else:
            sql = "SELECT COUNT(*) FROM track_neighbors"
            params = ()
        async with conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_neighbors_multi(
        self,
        track_path: str,
        edge_kinds: tuple[str, ...],
        k: int = 30,
    ) -> list[dict]:
        """Pool neighbours across multiple edge_kinds in one query. Returns
        rows with `path`, `weight`, `edge_kind`, `title`, `artist`, `album`.
        Caller is responsible for re-weighting by edge_kind and de-duplicating
        on `path` (the same track can appear in both acoustic and artist tiers).
        """
        if not edge_kinds:
            return []
        conn = await self.get_connection()
        placeholders = ",".join("?" * len(edge_kinds))
        sql = f'''
            SELECT n.neighbor_path AS path, n.weight, n.edge_kind,
                   t.title, ar.name AS artist, al.title AS album,
                   pc.cluster_id
            FROM track_neighbors n
            LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
            LEFT JOIN albums  al ON al.id      = t.album_id
            LEFT JOIN artists ar ON ar.id      = al.artist_id
            LEFT JOIN play_counts pc ON pc.track_path = n.neighbor_path
            WHERE n.track_path = ? AND n.edge_kind IN ({placeholders})
            ORDER BY n.weight DESC
            LIMIT ?
        '''
        params = (track_path, *edge_kinds, k)
        async with conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_neighbors_multi_batch(
        self,
        track_paths: list[str],
        edge_kinds: tuple[str, ...],
        k: int = 30,
    ) -> dict[str, list[dict]]:
        """Top-k pooled neighbours for MANY source paths in a single query.

        Replaces the walk's 2-hop prefetch loop (one round-trip per first-hop
        node — O(40) sequential awaits) with one window-function query. Returns
        {source_path: [neighbour rows]}; sources with no edges are simply
        absent. Row shape matches `get_neighbors_multi`.
        """
        if not track_paths or not edge_kinds:
            return {}
        conn = await self.get_connection()
        kind_ph = ",".join("?" * len(edge_kinds))
        out: dict[str, list[dict]] = {}
        # Chunk source paths to stay under SQLite's ~999 bound-parameter limit.
        for i in range(0, len(track_paths), 400):
            chunk = track_paths[i:i + 400]
            src_ph = ",".join("?" * len(chunk))
            # ROW_NUMBER() partitions by source so each source keeps only its
            # own top-k by weight — the per-source LIMIT the loop did serially.
            sql = f'''
                WITH ranked AS (
                    SELECT n.track_path AS src, n.neighbor_path AS path,
                           n.weight, n.edge_kind,
                           t.title, ar.name AS artist, al.title AS album,
                           pc.cluster_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY n.track_path ORDER BY n.weight DESC
                           ) AS rn
                    FROM track_neighbors n
                    LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
                    LEFT JOIN albums  al ON al.id      = t.album_id
                    LEFT JOIN artists ar ON ar.id      = al.artist_id
                    LEFT JOIN play_counts pc ON pc.track_path = n.neighbor_path
                    WHERE n.track_path IN ({src_ph})
                      AND n.edge_kind IN ({kind_ph})
                )
                SELECT src, path, weight, edge_kind, title, artist, album,
                       cluster_id
                FROM ranked
                WHERE rn <= ?
            '''
            params = (*chunk, *edge_kinds, k)
            async with conn.execute(sql, params) as cursor:
                for r in await cursor.fetchall():
                    d = dict(r)
                    out.setdefault(d.pop("src"), []).append(d)
        return out

    # ── Playback history (long-term avoid + listen feedback) ────────────────

    async def record_playback(
        self,
        path: str,
        event: str = "played",
        seed_path: str | None = None,
    ) -> None:
        """Append a single playback event. `event` is one of 'played'
        (track started), 'completed' (played past the listen-signal threshold)
        or 'skipped_early' (cut off well before completion)."""
        if not path:
            return
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                "INSERT INTO playback_history (track_path, played_at, event, seed_path) "
                "VALUES (?, strftime('%s','now'), ?, ?)",
                (path, event, seed_path),
            )
            await conn.commit()

    async def recent_played_paths(self, window_seconds: int = 7 * 86400) -> set[str]:
        """Distinct track paths that have a 'played' event within the last
        `window_seconds`. Used by the assistant as a long-term avoid set so
        "play similar" doesn't repeat tracks across app restarts."""
        conn = await self.get_connection()
        sql = (
            "SELECT DISTINCT track_path FROM playback_history "
            "WHERE event = 'played' "
            "AND played_at >= strftime('%s','now') - ?"
        )
        async with conn.execute(sql, (window_seconds,)) as cursor:
            return {r[0] for r in await cursor.fetchall()}

    async def get_recent_tracks(self, limit: int = 15) -> list[dict]:
        """Distinct tracks most-recently 'played', newest first, with metadata.
        Unlike recent_played_paths (an unordered set for the avoid-list), this
        preserves recency order so the assistant can answer 'what did I just
        play' / 'play what I was listening to earlier'."""
        conn = await self.get_connection()
        sql = '''
            SELECT t.path, t.title, ar.name AS artist, al.title AS album, al.genre,
                   MAX(ph.played_at) AS last_played
            FROM playback_history ph
            JOIN tracks t   ON t.path = ph.track_path
            JOIN albums al  ON al.id  = t.album_id
            JOIN artists ar ON ar.id  = al.artist_id
            WHERE ph.event = 'played'
            GROUP BY t.path
            ORDER BY last_played DESC
            LIMIT ?
        '''
        async with conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_track_full(self, path: str) -> dict | None:
        """Single-row lookup returning title/artist/album/genre and the DSP
        timbre BLOB for a path. The LEFT JOIN on play_counts means callers
        get `row["timbre"]` for free without a second query — used by
        islet creation (Jarvis + Library dialog) where we need the timbre
        vector immediately after grabbing the metadata."""
        conn = await self.get_connection()
        sql = '''
            SELECT t.path, t.title, t.duration, t.format, t.bpm, t.energy, t.brightness,
                   ar.name AS artist, al.title AS album, al.year, al.genre,
                   pc.timbre
            FROM tracks t
            LEFT JOIN albums      al ON al.id          = t.album_id
            LEFT JOIN artists     ar ON ar.id          = al.artist_id
            LEFT JOIN play_counts pc ON pc.track_path  = t.path
            WHERE t.path = ?
        '''
        async with conn.execute(sql, (path,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def search_tracks_simple(self, query: str, limit: int = 5) -> list[dict]:
        """Lightweight title/artist/album LIKE search for the assistant. Falls
        back to the FTS-aware `get_all_tracks` when available — but this
        method's return shape is the same `path/title/artist/album` dict the
        assistant uses everywhere, so callers don't need to remap."""
        q = (query or "").strip()
        if not q:
            return []
        conn = await self.get_connection()

        # 1. First attempt: Split by "by" or "from" (e.g. "comfortably numb by pink floyd")
        by_parts = re.split(r"\s+(?:by|from)\s+", q, flags=re.I)
        if len(by_parts) == 2:
            p1, p2 = by_parts[0].strip(), by_parts[1].strip()
            if p1 and p2:
                like1 = f"%{p1}%"
                like2 = f"%{p2}%"
                sql = '''
                    SELECT t.path, t.title, t.duration,
                           ar.name AS artist, al.title AS album
                    FROM tracks t
                    LEFT JOIN albums  al ON al.id     = t.album_id
                    LEFT JOIN artists ar ON ar.id     = al.artist_id
                    WHERE (t.title LIKE ? COLLATE NOCASE AND ar.name LIKE ? COLLATE NOCASE)
                       OR (t.title LIKE ? COLLATE NOCASE AND ar.name LIKE ? COLLATE NOCASE)
                    ORDER BY t.added_date DESC
                    LIMIT ?
                '''
                async with conn.execute(sql, (like1, like2, like2, like1, limit)) as cursor:
                    results = [dict(r) for r in await cursor.fetchall()]
                    if results:
                        return results

        # 2. Second attempt: Multi-word intersection query (matches all words across fields in any order)
        stop_words = {"a", "an", "the", "by", "from", "of", "in", "on", "at", "to", "and", "or", "for", "some", "any"}
        words = [w for w in q.split() if w.lower() not in stop_words]
        if not words:
            words = q.split()  # fallback if all were stop words

        if words:
            clauses = []
            params = []
            for w in words:
                clauses.append("(t.title LIKE ? COLLATE NOCASE OR ar.name LIKE ? COLLATE NOCASE OR al.title LIKE ? COLLATE NOCASE)")
                like_w = f"%{w}%"
                params.extend([like_w, like_w, like_w])

            where_clause = " AND ".join(clauses)
            sql = f'''
                SELECT t.path, t.title, t.duration,
                       ar.name AS artist, al.title AS album
                FROM tracks t
                LEFT JOIN albums  al ON al.id     = t.album_id
                LEFT JOIN artists ar ON ar.id     = al.artist_id
                WHERE {where_clause}
                ORDER BY
                    CASE
                        WHEN t.title LIKE ? COLLATE NOCASE THEN 0
                        WHEN ar.name LIKE ? COLLATE NOCASE THEN 1
                        ELSE 2
                    END,
                    t.added_date DESC
                LIMIT ?
            '''
            exact_like = f"%{q}%"
            params.extend([exact_like, exact_like, limit])

            async with conn.execute(sql, params) as cursor:
                results = [dict(r) for r in await cursor.fetchall()]
                if results:
                    return results

        # 3. Third attempt: Fallback to single rigid LIKE match (original behavior)
        like = f"%{q}%"
        sql = '''
            SELECT t.path, t.title, t.duration,
                   ar.name AS artist, al.title AS album
            FROM tracks t
            LEFT JOIN albums  al ON al.id     = t.album_id
            LEFT JOIN artists ar ON ar.id     = al.artist_id
            WHERE t.title  LIKE ? COLLATE NOCASE
               OR ar.name  LIKE ? COLLATE NOCASE
               OR al.title LIKE ? COLLATE NOCASE
            ORDER BY
                CASE
                    WHEN t.title  LIKE ? COLLATE NOCASE THEN 0
                    WHEN ar.name  LIKE ? COLLATE NOCASE THEN 1
                    ELSE 2
                END,
                t.added_date DESC
            LIMIT ?
        '''
        async with conn.execute(sql, (like, like, like, like, like, limit)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def debug_populate_play_counts(self):
        """
        Randomly assigns play counts so the AutoPlaylist UI has data to work
        with on a fresh install. Does NOT synthesise audio features any more;
        the random values were poisoning the KNN selector (every playlist
        was just a slice of uniform noise). The DSP analyser populates real
        features lazily for the hot set the first time a playlist is generated.
        """
        async with self._write_lock:
            conn = await self.get_connection()
            async with conn.execute("SELECT path FROM tracks") as cursor:
                tracks = await cursor.fetchall()

            import random
            data = []
            for row in tracks:
                path = row[0]
                count = random.randint(1, 1000)
                last_played_offset = random.randint(0, 3600 * 24 * 30)
                data.append((path, count, last_played_offset))

            await conn.executemany('''
                INSERT INTO play_counts (track_path, count, last_played)
                VALUES (?, ?, strftime('%s','now') - ?)
                ON CONFLICT(track_path) DO UPDATE SET
                    count = EXCLUDED.count,
                    last_played = EXCLUDED.last_played
            ''', data)
            await conn.commit()

    # ─── Acoustic geometry schema ─────────────────────────────────────────────

    # Bump when the Zr geometry changes shape or meaning, so an upgrading DB
    # discards coordinates that were built by the old pipeline. Stored in
    # SQLite's built-in PRAGMA user_version.
    #   1 — unified graph Zr (~20-D) replacing the original 3-D PCA coords
    #   2 — harmonic late-fusion block deleted (Zr ~21-D → ~17-D)
    GEOMETRY_VERSION = 2

    async def _migrate_pca(self, conn):
        """Idempotent: ensure play_counts.pca_coords exists, and drop stored
        coordinates whenever GEOMETRY_VERSION has moved past what built them.

        There used to be a `pca_space` table persisting the projection matrix,
        means/stds and a feature spec so a new track could be projected into the
        existing Zr space without a rebuild. Nothing ever read it back — the
        `project_to_zr` function its docstring referenced was never written — so
        it was a write-only table, and the geometry bump rode on a side effect of
        ALTER TABLE failing the second time. Both are gone; the version stamp
        below does that job explicitly.
        """
        try:
            await conn.execute("ALTER TABLE play_counts ADD COLUMN pca_coords BLOB")
        except Exception:
            pass  # Column already exists (or play_counts isn't created yet)
        await conn.execute("DROP TABLE IF EXISTS pca_space")

        async with conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        current = int(row[0]) if row else 0
        if current < self.GEOMETRY_VERSION:
            # NB this migration runs from get_connection(), i.e. BEFORE
            # initialize() creates the schema, so on a fresh DB there is nothing
            # to clear — we only stamp the version.
            cleared = 0
            try:
                cur = await conn.execute("UPDATE play_counts SET pca_coords = NULL")
                cleared = cur.rowcount or 0
            except Exception:
                pass  # table not created yet
            await conn.execute(f"PRAGMA user_version = {self.GEOMETRY_VERSION}")
            if cleared:
                logger.info(
                    "db: acoustic geometry v%d -> v%d; cleared %d stored Zr "
                    "coords, the next graph build will repopulate them.",
                    current, self.GEOMETRY_VERSION, cleared,
                )
        await conn.commit()

    async def update_track_pca_coords(self, track_path: str, coords: np.ndarray):
        """Mutation: Caches the 3D PC coordinates for a track in play_counts."""
        import numpy as np
        coords_bytes = coords.astype(np.float32).tobytes()
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute('''
                UPDATE play_counts
                SET pca_coords = ?
                WHERE track_path = ?
            ''', (coords_bytes, track_path))
            await conn.commit()

    async def update_tracks_pca_coords_batch(self, batch_data: list[tuple[str, np.ndarray]]):
        """Mutation: Efficiently updates PCA coordinates for a batch of tracks."""
        import numpy as np
        formatted_data = [
            (coords.astype(np.float32).tobytes(), path)
            for path, coords in batch_data
        ]
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.executemany('''
                UPDATE play_counts
                SET pca_coords = ?
                WHERE track_path = ?
            ''', formatted_data)
            await conn.commit()
            logger.info(f"Batched {len(batch_data)} PCA coordinate updates in play_counts.")

    async def get_tracks_pca_coords(self) -> list[dict]:
        """Lock-free read. Returns tracks joined with metadata and their projected 3D coordinates."""
        import numpy as np
        conn = await self.get_connection()
        sql = '''
            SELECT pc.track_path AS path, pc.pca_coords, pc.cluster_id, pc.bpm, pc.energy,
                   t.title, t.duration,
                   ar.name  AS artist,
                   al.title AS album,
                   al.genre AS genre
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path  = pc.track_path
            LEFT JOIN albums  al ON al.id   = t.album_id
            LEFT JOIN artists ar ON ar.id   = al.artist_id
            WHERE pc.pca_coords IS NOT NULL
        '''
        async with conn.execute(sql) as cursor:
            results = []
            for r in await cursor.fetchall():
                row_dict = dict(r)
                try:
                    row_dict["pca_coords"] = np.frombuffer(row_dict["pca_coords"], dtype=np.float32).tolist()
                except Exception:
                    row_dict["pca_coords"] = None  # variable-dim now; let callers skip/re-project
                results.append(row_dict)
            return results

    async def get_tracks_pca_coords_for_paths(self, paths: list[str]) -> list[dict]:
        """Fetch PCA coords + metadata for a specific set of track paths only.
        Much faster than get_tracks_pca_coords() for small path sets (Local/Walk
        network views need ~15 paths vs. potentially thousands in the full library)."""
        if not paths:
            return []
        import numpy as np
        conn = await self.get_connection()
        placeholders = ",".join("?" for _ in paths)
        sql = f'''
            SELECT pc.track_path AS path, pc.pca_coords, pc.cluster_id, pc.bpm, pc.energy,
                   pc.count AS play_count,
                   t.title, t.duration,
                   ar.name  AS artist,
                   al.title AS album,
                   al.genre AS genre
            FROM play_counts pc
            INNER JOIN tracks  t  ON t.path  = pc.track_path
            LEFT JOIN albums  al ON al.id   = t.album_id
            LEFT JOIN artists ar ON ar.id   = al.artist_id
            WHERE pc.track_path IN ({placeholders})
              AND pc.pca_coords IS NOT NULL
        '''
        async with conn.execute(sql, paths) as cursor:
            results = []
            for r in await cursor.fetchall():
                row_dict = dict(r)
                try:
                    row_dict["pca_coords"] = np.frombuffer(row_dict["pca_coords"], dtype=np.float32).tolist()
                except Exception:
                    row_dict["pca_coords"] = None
                results.append(row_dict)
            return results
    async def has_pca_coords(self) -> bool:
        """Fast existence check — no blob deserialization."""
        conn = await self.get_connection()
        sql = """
            SELECT 1 
            FROM play_counts pc
            INNER JOIN tracks t ON t.path = pc.track_path
            WHERE pc.pca_coords IS NOT NULL LIMIT 1
        """
        async with conn.execute(sql) as cursor:
            return (await cursor.fetchone()) is not None

    # ── Track Clustering ───────────────────────────────────────────────────────

    async def _migrate_clusters(self, conn):
        """Idempotent migration: adds the cluster_id column to play_counts
        (Louvain community per track, consumed by the walk's cross-cluster
        penalty)."""
        try:
            await conn.execute(
                "ALTER TABLE play_counts ADD COLUMN cluster_id INTEGER"
            )
            await conn.commit()
        except Exception:
            pass  # Column already exists

    async def _migrate_album_genre_bucket(self, conn):
        """Idempotent migration: adds the `genre_bucket` column to albums — the
        coarse family (pca_engine.genre_bucket) kept ALONGSIDE the specific
        display `genre`, so grouping/colour has a canonical key without the
        display tag having to be flattened to it."""
        try:
            await conn.execute("ALTER TABLE albums ADD COLUMN genre_bucket TEXT")
            await conn.commit()
        except Exception:
            pass  # Column already exists

    async def save_track_clusters(
        self, path_cluster_pairs: list[tuple[str, int]]
    ) -> None:
        """Batch-update cluster_id in play_counts for every (path, cluster_id)
        pair. Called once per graph rebuild."""
        if not path_cluster_pairs:
            return
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.executemany(
                "UPDATE play_counts SET cluster_id = ? WHERE track_path = ?",
                [(cid, path) for path, cid in path_cluster_pairs],
            )
            await conn.commit()
            logger.info(
                "Saved cluster assignments for %d tracks.",
                len(path_cluster_pairs),
            )

    async def get_track_cluster(self, path: str) -> int | None:
        """Single-row cluster lookup."""
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT cluster_id FROM play_counts WHERE track_path = ?",
            (path,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None

    # ── Artist metadata enrichment (provenance / fine genre) ──────────────────
    # A standalone cache table: external lookups (MusicBrainz etc.) keyed by
    # artist name. Deliberately a *new* table — never an ALTER on tracks/albums —
    # so shipping this to an existing phone DB only creates an empty table and
    # NEVER touches acoustic features, pca_coords or neighbours (no recompute).
    async def _migrate_enrichment(self, conn):
        """Idempotent migration: create the artist_enrichment cache table.

        Purely additive. `CREATE TABLE IF NOT EXISTS` is a no-op on a DB that
        already has it and harmless on one that doesn't, so an upgrade install
        can never break or force a re-analysis of the existing library."""
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS artist_enrichment (
                artist_name TEXT PRIMARY KEY COLLATE NOCASE,
                mbid        TEXT,
                country     TEXT,
                area        TEXT,
                genres      TEXT,     -- JSON: [{"name": str, "count": int}, ...]
                source      TEXT,     -- e.g. 'musicbrainz'
                score       INTEGER,  -- match confidence (0-100) from the search
                status      TEXT,     -- 'ok' | 'lowconfidence' | 'notfound' | 'error'
                fetched_at  REAL
            )
        ''')
        # NPMI genre-similarity model, precomputed at graph generation and read
        # by the walk's metadata gate. Single-row JSON blob (small + atomic),
        # Additive: harmless on a DB that predates it.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS genre_affinity (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                model      TEXT NOT NULL,   -- JSON {"a|b": npmi, ...}, a < b
                updated_at REAL NOT NULL
            )
        ''')
        # Genre-adjacency graph (PAGA nodes + adjacency) driving the journey
        # walk, precomputed at graph generation. Single-row JSON blob:
        # {"version", "nodes": {path: node}, "adj": {node: [[node, lift], ...]}}.
        # Additive: harmless on a DB that predates it; absent -> walk falls back
        # to the pure-radius ranking.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS genre_graph (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                model      TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        ''')
        await conn.commit()

    async def _migrate_features_v4_to_v5(self, conn):
        """
        Migrates feature_cache and play_counts timbre blobs from v4 (88 floats)
        to v5 (68 floats) by dropping the 20-dim mfcc_delta block at indices [40:60).
        """
        import numpy as np
        N_MFCC = 20
        # 1. Migrate play_counts table
        try:
            cursor = await conn.execute(
                "SELECT track_path, timbre FROM play_counts WHERE timbre IS NOT NULL AND features_version = 4"
            )
            rows = await cursor.fetchall()
            n = 0
            for path, blob in rows:
                if blob and len(blob) == 88 * 4:  # v4 blob length (352 bytes)
                    v = np.frombuffer(blob, dtype="<f4")
                    v5 = np.delete(v, np.s_[2 * N_MFCC:3 * N_MFCC]).astype("<f4").tobytes()
                    await conn.execute(
                        "UPDATE play_counts SET timbre = ?, features_version = 5 WHERE track_path = ?",
                        (v5, path)
                    )
                    n += 1
            if n > 0:
                await conn.commit()
                logger.info(f"DatabaseManager: Migrated {n} tracks in play_counts to features_version = 5")
        except Exception as e:
            logger.error(f"Failed to migrate play_counts to v5: {e}")

        # 2. Migrate feature_cache table
        try:
            cursor = await conn.execute(
                "SELECT track_path, timbre FROM feature_cache WHERE timbre IS NOT NULL AND features_version = 4"
            )
            rows = await cursor.fetchall()
            n = 0
            for path, blob in rows:
                if blob and len(blob) == 88 * 4:  # v4 blob length (352 bytes)
                    v = np.frombuffer(blob, dtype="<f4")
                    v5 = np.delete(v, np.s_[2 * N_MFCC:3 * N_MFCC]).astype("<f4").tobytes()
                    await conn.execute(
                        "UPDATE feature_cache SET timbre = ?, features_version = 5 WHERE track_path = ?",
                        (v5, path)
                    )
                    n += 1
            if n > 0:
                await conn.commit()
                logger.info(f"DatabaseManager: Migrated {n} tracks in feature_cache to features_version = 5")
        except Exception as e:
            logger.error(f"Failed to migrate feature_cache to v5: {e}")

    async def get_artist_enrichment(self, name: str) -> dict | None:
        """Lock-free read of one artist's cached enrichment, or None if absent.
        `genres` is decoded back to a list; other columns pass through."""
        if not name:
            return None
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT artist_name, mbid, country, area, genres, source, score, status, fetched_at "
            "FROM artist_enrichment WHERE artist_name = ?",
            (name,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["genres"] = json.loads(out["genres"]) if out["genres"] else []
        except Exception:
            out["genres"] = []
        return out

    async def upsert_artist_enrichment(
        self, artist_name: str, *, mbid: str | None = None,
        country: str | None = None, area: str | None = None,
        genres=None, source: str = "musicbrainz",
        score: int | None = None, status: str = "ok",
        force: bool = False,
    ) -> None:
        """Mutation: insert/replace one artist's enrichment row. `genres` may be a
        list (stored as JSON) or None. One row per artist; cheap to re-run.
        Guards existing source='manual' rows from being overwritten unless source is 'manual' or force is True."""
        genres_text = json.dumps(genres) if genres is not None else None
        async with self._write_lock:
            conn = await self.get_connection()
            if not force and source != "manual":
                async with conn.execute(
                    "SELECT source FROM artist_enrichment WHERE artist_name = ?",
                    (artist_name,),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing and existing[0] == "manual":
                    logger.info("Preserving manual override for artist: %s", artist_name)
                    return
            await conn.execute('''
                INSERT OR REPLACE INTO artist_enrichment
                    (artist_name, mbid, country, area, genres, source, score, status, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
            ''', (artist_name, mbid, country, area, genres_text, source, score, status))
            await conn.commit()

    async def set_manual_artist_enrichment(
        self, artist_name: str, *, country: str | None = None,
        genres: list[str] | list[dict] | None = None,
    ) -> None:
        """User-entered override. Stored as source='manual', status='ok' so it feeds
        the genre model and is never overwritten by automated MusicBrainz syncs."""
        genre_objs = []
        if genres:
            for g in genres:
                if isinstance(g, dict):
                    genre_objs.append(g)
                elif isinstance(g, str) and g.strip():
                    genre_objs.append({"name": g.strip(), "count": 1})
        await self.upsert_artist_enrichment(
            artist_name, country=(country or None), genres=genre_objs,
            source="manual", status="ok", score=100, force=True,
        )

    async def get_genre_vocabulary(self, limit: int = 60) -> list[dict]:
        """The genre tokens this library already uses, most-attested first, as
        [{'name', 'artists'}]. Powers tap-to-add suggestions when hand-tagging
        an artist the automatic enrichment could not resolve.

        This is a correctness feature, not just a typing shortcut. The walk
        compares tags through the NPMI model in `genre_similarity`, which is
        learned from co-occurrence across THIS library's artists — a token that
        appears on exactly one artist has no learned relation to anything, so a
        freshly invented spelling ('greek trap' where the corpus says 'trap')
        lands at the FAMILY_FLOOR at best and can be fenced apart from the very
        scene it belongs to. Offering the established vocabulary keeps
        hand-entered tags inside the model that has to interpret them."""
        conn = await self.get_connection()
        counts: dict[str, int] = {}
        display: dict[str, str] = {}
        async with conn.execute(
            "SELECT genres FROM artist_enrichment "
            "WHERE genres IS NOT NULL AND genres <> '' AND genres <> '[]'"
        ) as cursor:
            rows = await cursor.fetchall()
        for (genres,) in rows:
            seen: set[str] = set()
            try:
                parsed = json.loads(genres or "[]")
            except Exception:
                continue
            for g in parsed:
                name = (g.get("name") if isinstance(g, dict) else str(g)) or ""
                name = name.strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                counts[key] = counts.get(key, 0) + 1
                display.setdefault(key, name)
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"name": display[k], "artists": c} for k, c in ordered[:limit]]

    async def get_artist_source_genres(self, artist_name: str) -> list[str]:
        """Genre strings the FILES themselves carry for this artist, split into
        individual tokens (`albums.genre` is a comma/slash-joined source tag
        list). These are the most relevant suggestions for a hand-tagging pass:
        the download source usually knew the artist was 'Rap' even when
        MusicBrainz has no entry for them at all.

        Display-only junk is dropped — placeholder buckets ('Divers',
        'Various', 'Unknown') carry no genre information, and the French
        localisations some sources emit are folded to their English token so
        they join the corpus vocabulary instead of forking it."""
        _JUNK = {"divers", "various", "unknown", "other", "misc", "n/a"}
        _LOCALISED = {
            "électronique": "Electronic", "electronique": "Electronic",
            "danse": "Dance", "musique du monde": "World",
            "bandes originales": "Soundtrack",
        }
        conn = await self.get_connection()
        async with conn.execute(
            "SELECT al.genre FROM albums al JOIN artists ar ON ar.id = al.artist_id "
            "WHERE ar.name = ? AND al.genre IS NOT NULL AND al.genre <> ''",
            (artist_name,),
        ) as cursor:
            rows = await cursor.fetchall()
        out: list[str] = []
        seen: set[str] = set()
        for (genre_str,) in rows:
            for part in re.split(r"[,/;|]", genre_str or ""):
                tok = part.strip()
                if not tok:
                    continue
                low = tok.lower()
                if low in _JUNK:
                    continue
                tok = _LOCALISED.get(low, tok)
                key = tok.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(tok)
        return out

    async def get_metadata_gap_artists(self, limit: int = 200) -> list[dict]:
        """Artists whose enrichment is missing genres and/or country, excluding
        manual rows. Powers the wizard failure/gap resolution list.

        Ordered by how much the gap HURTS the walk, not just by track count:

          1. no genres AND no country — completely invisible to the walk's pool
             gate. Both `_pool_foreign` boundaries need evidence, so such a
             track can never be fenced out of any queue: this is the case that
             put Greek laiko in the middle of a Slipknot queue. Nothing but a
             hand entry can fix it.
          2. no genres (country known) — the genre boundary is dark, and the
             seed side now falls back to the country fence, so these degrade
             but are not unbounded.
          3. country missing only — genres still gate the pool; least harmful.

        Track count breaks ties within each tier, so the artist you actually
        listen to comes first. Ordering matters here because the list is a
        manual work queue and nobody gets to the bottom of it."""
        conn = await self.get_connection()
        sql = (
            "SELECT a.name AS artist_name, a.track_count AS track_count, "
            "e.country AS country, e.genres AS genres, e.source AS source, e.status AS status, "
            "CASE "
            "  WHEN (e.genres IS NULL OR e.genres = '' OR e.genres = '[]') "
            "       AND (e.country IS NULL OR e.country = '') THEN 0 "
            "  WHEN (e.genres IS NULL OR e.genres = '' OR e.genres = '[]') THEN 1 "
            "  ELSE 2 "
            "END AS gap_severity "
            "FROM artists a LEFT JOIN artist_enrichment e ON e.artist_name = a.name "
            "WHERE (e.artist_name IS NULL OR e.genres IS NULL OR e.genres = '' OR e.genres = '[]' "
            "       OR e.country IS NULL OR e.country = '') "
            "  AND (e.source IS NULL OR e.source <> 'manual') "
            "ORDER BY gap_severity ASC, a.track_count DESC LIMIT ?"
        )
        async with conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["genres"] = json.loads(d["genres"]) if d["genres"] else []
            except Exception:
                d["genres"] = []
            out.append(d)
        return out

    async def get_metadata_coverage(self) -> dict:
        """Library-wide metadata health, for the workbench summary.

        The headline is TRACK-level genre coverage, because that is the field
        the walk's pool gate reads first (the genre boundary); country is the
        fallback the untagged-seed / regional fence uses. Artist-level severity
        counts mirror `get_metadata_gap_artists` so the summary and the list
        agree on what 'critical' means."""
        conn = await self.get_connection()

        async def scalar(sql: str) -> int:
            async with conn.execute(sql) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0

        total = await scalar("SELECT COUNT(*) FROM tracks")
        base = (
            "FROM tracks t JOIN albums al ON al.id=t.album_id "
            "JOIN artists ar ON ar.id=al.artist_id "
            "LEFT JOIN artist_enrichment e ON e.artist_name=ar.name "
        )
        _has_g = "e.genres IS NOT NULL AND e.genres<>'' AND e.genres<>'[]'"
        _has_c = "e.country IS NOT NULL AND e.country<>''"
        with_genres = await scalar("SELECT COUNT(*) " + base + "WHERE " + _has_g)
        with_country = await scalar("SELECT COUNT(*) " + base + "WHERE " + _has_c)
        # Track-level THREE-WAY partition for the coverage bar, so green+amber+red
        # sum to `total` exactly (the old bar mixed a track-level green with an
        # artist-level red and did not add up):
        #   green  = has genres            (walk-ready — the gate's primary field)
        #   red    = has neither           (critical — unfenceable)
        #   amber  = the remainder         (has country but no genres)
        tracks_critical = await scalar(
            "SELECT COUNT(*) " + base + f"WHERE NOT ({_has_g}) AND NOT ({_has_c})"
        )
        total_artists = await scalar("SELECT COUNT(*) FROM artists")

        gaps = await self.get_metadata_gap_artists(limit=1_000_000)
        from collections import Counter
        sev = Counter(g.get("gap_severity", 2) for g in gaps)
        return {
            "tracks": total,
            "tracks_with_genres": with_genres,
            "tracks_with_country": with_country,
            "tracks_critical": tracks_critical,
            "tracks_partial": max(0, total - with_genres - tracks_critical),
            "genre_pct": (with_genres / total) if total else 0.0,
            "country_pct": (with_country / total) if total else 0.0,
            "artists": total_artists,
            "gap_artists": len(gaps),
            "critical": sev.get(0, 0),   # no genres AND no country
            "no_genres": sev.get(1, 0),  # no genres, country known
            "no_country": sev.get(2, 0), # genres known, no country
        }

    async def set_manual_artist_enrichment_bulk(
        self, artist_names: list[str], *, country: str | None = None,
        genres: list[str] | None = None, refresh_model: bool = True,
    ) -> int:
        """Apply the SAME manual override to many artists at once — the batch
        move for a whole scene (e.g. a shelf of Greek artists that are all GR +
        trap/hip hop). Rebuilds the NPMI genre model ONCE at the end rather than
        per artist. Returns the number of artists written."""
        n = 0
        for name in artist_names:
            try:
                await self.set_manual_artist_enrichment(
                    name, country=country, genres=genres,
                )
                n += 1
            except Exception as exc:
                logger.warning("bulk manual enrichment failed for %s: %s", name, exc)
        if refresh_model and n:
            try:
                from utils.track_graph import build_genre_affinity, build_journey_graph
                await build_genre_affinity(self)
                # Country/genre edits move nodes and regional splits, so refresh
                # the journey graph too (coords are unchanged, so this is cheap).
                await build_journey_graph(self)
            except Exception as exc:
                logger.debug("genre model refresh after bulk override failed: %s", exc)
        return n

    async def get_low_confidence_artists(self, limit: int = 200) -> list[dict]:
        """Artists whose enrichment has status='lowconfidence'. Powers the wizard match resolution list."""
        conn = await self.get_connection()
        sql = (
            "SELECT a.name AS artist_name, a.track_count AS track_count, "
            "e.mbid AS mbid, e.country AS country, e.area AS area, e.genres AS genres, "
            "e.source AS source, e.score AS score, e.status AS status "
            "FROM artists a JOIN artist_enrichment e ON e.artist_name = a.name "
            "WHERE e.status = 'lowconfidence' "
            "ORDER BY a.track_count DESC LIMIT ?"
        )
        async with conn.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["genres"] = json.loads(d["genres"]) if d["genres"] else []
            except Exception:
                d["genres"] = []
            out.append(d)
        return out

    async def confirm_artist_match(
        self, artist_name: str, *, mbid: str | None = None,
        country: str | None = None, area: str | None = None,
        genres=None, status: str = "ok", score: int = 100,
    ) -> None:
        """Promote or resolve an artist match, marking it as confirmed status='ok' (or 'notfound')."""
        await self.upsert_artist_enrichment(
            artist_name, mbid=mbid, country=country, area=area,
            genres=genres, source="musicbrainz", score=score, status=status, force=True,
        )

    async def get_artists_needing_enrichment(
        self, limit: int | None = None, include_failed: bool = False,
    ) -> list[str]:
        """Lock-free read: distinct artist names that have no cached enrichment
        yet (or, with include_failed, also those whose last attempt errored /
        wasn't found / came back INCOMPLETE — empty genres). The incomplete case
        lets a re-sync heal rows the old MusicBrainz search populated from the
        wrong entity (e.g. a tribute band → empty genres). Rows the user filled
        by hand (source='manual') are never re-fetched. Drives the batch pass."""
        conn = await self.get_connection()
        if include_failed:
            # Re-fetch artists with no enrichment row OR whose last attempt errored (transient failures).
            # Processed rows (status='ok', 'lowconfidence', 'notfound') are preserved so sync decreases monotonically.
            sql = (
                "SELECT a.name FROM artists a "
                "LEFT JOIN artist_enrichment e ON e.artist_name = a.name "
                "WHERE (e.artist_name IS NULL OR e.status = 'error') "
                "  AND (e.source IS NULL OR e.source <> 'manual') "
                "ORDER BY a.track_count DESC"
            )
        else:
            sql = (
                "SELECT a.name FROM artists a "
                "LEFT JOIN artist_enrichment e ON e.artist_name = a.name "
                "WHERE e.artist_name IS NULL "
                "ORDER BY a.track_count DESC"
            )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        async with conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows if r[0]]

    async def get_artist_meta_for_paths(self, paths: list[str]) -> dict:
        """Lock-free read powering the walk's metadata-fusion gate. Returns
        {path: {'artist', 'country', 'genres'}} where `genres` is a frozenset of
        canonicalised genre tokens (possibly empty). Paths whose artist has no
        enrichment row still come back with artist + empty provenance, so the
        caller can apply the same-artist exclusion. Chunked to stay under
        SQLite's bound-variable limit."""
        if not paths:
            return {}
        conn = await self.get_connection()
        out: dict = {}
        CHUNK = 400
        for i in range(0, len(paths), CHUNK):
            chunk = paths[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            sql = (
                "SELECT t.path AS path, ar.name AS artist, "
                "e.country AS country, e.genres AS genres "
                "FROM tracks t "
                "JOIN albums al ON al.id = t.album_id "
                "JOIN artists ar ON ar.id = al.artist_id "
                "LEFT JOIN artist_enrichment e ON e.artist_name = ar.name "
                f"WHERE t.path IN ({placeholders})"
            )
            async with conn.execute(sql, chunk) as cursor:
                for row in await cursor.fetchall():
                    toks = set()
                    if row["genres"]:
                        try:
                            for g in json.loads(row["genres"]):
                                tk = "".join(
                                    c for c in (g.get("name", "") or "").lower()
                                    if c.isalnum()
                                )
                                if tk:
                                    toks.add(tk)
                        except Exception:
                            pass
                    out[row["path"]] = {
                        "artist": row["artist"],
                        "country": row["country"],
                        "genres": frozenset(toks),
                    }
        return out

    async def get_all_artist_genre_sets(self) -> list:
        """Lock-free read: one canonicalised genre-token set per enriched artist,
        the co-occurrence corpus for the NPMI genre model. Empty sets dropped."""
        conn = await self.get_connection()
        out: list = []
        async with conn.execute(
            "SELECT genres FROM artist_enrichment "
            "WHERE genres IS NOT NULL AND status IN ('ok', 'lowconfidence')"
        ) as cursor:
            for (genres,) in await cursor.fetchall():
                toks = set()
                try:
                    for g in json.loads(genres or "[]"):
                        tk = "".join(
                            c for c in (g.get("name", "") or "").lower()
                            if c.isalnum()
                        )
                        if tk:
                            toks.add(tk)
                except Exception:
                    pass
                if toks:
                    out.append(toks)
        return out

    async def save_genre_affinity(self, model: dict) -> None:
        """Mutation: persist the NPMI genre model (single-row JSON) + refresh the
        in-memory cache. Called at graph generation."""
        model = model or {}
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                "INSERT OR REPLACE INTO genre_affinity (id, model, updated_at) "
                "VALUES (1, ?, strftime('%s','now'))",
                (json.dumps(model),),
            )
            await conn.commit()
        self._genre_affinity_cache = model
        logger.info("Genre affinity model persisted (%d pairs).", len(model))

    async def get_genre_affinity(self) -> dict:
        """Lock-free read of the persisted NPMI genre model, memoised after the
        first hit (it's rebuilt — and the cache refreshed — only at graph gen).
        Returns {} when unset, which makes soft_set_sim degrade to Dice."""
        if self._genre_affinity_cache is not None:
            return self._genre_affinity_cache
        conn = await self.get_connection()
        model: dict = {}
        async with conn.execute(
            "SELECT model FROM genre_affinity WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            try:
                model = json.loads(row[0])
            except Exception:
                model = {}
        self._genre_affinity_cache = model
        return model

    async def save_journey_graph(self, payload: dict) -> None:
        """Mutation: persist the genre-adjacency graph (single-row JSON) + refresh
        the in-memory cache. Called at graph generation, right after the NPMI
        model. An empty payload stores an empty object, which the walk reads as
        'no graph' and falls back to the radius ranking."""
        payload = payload or {}
        async with self._write_lock:
            conn = await self.get_connection()
            await conn.execute(
                "INSERT OR REPLACE INTO genre_graph (id, model, updated_at) "
                "VALUES (1, ?, strftime('%s','now'))",
                (json.dumps(payload),),
            )
            await conn.commit()
        self._genre_graph_cache = payload
        n_nodes = len(payload.get("nodes", {})) if payload else 0
        logger.info("Genre-adjacency graph persisted (%d placed tracks).", n_nodes)

    async def get_journey_graph(self) -> dict:
        """Lock-free read of the persisted genre-adjacency graph, memoised after
        the first hit (rebuilt — and the cache refreshed — only at graph gen).
        Returns {} when unset, which makes the walk degrade to the radius."""
        if self._genre_graph_cache is not None:
            return self._genre_graph_cache
        conn = await self.get_connection()
        payload: dict = {}
        async with conn.execute(
            "SELECT model FROM genre_graph WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0]:
            try:
                payload = json.loads(row[0])
            except Exception:
                payload = {}
        self._genre_graph_cache = payload
        return payload

    async def get_track_clusters_bulk(
        self, paths: list[str]
    ) -> dict[str, int | None]:
        """Bulk cluster lookup. Returns {path: cluster_id} for every path in
        the input list. Missing / un-clustered tracks map to None."""
        if not paths:
            return {}
        conn = await self.get_connection()
        out: dict[str, int | None] = {p: None for p in paths}
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            sql = (
                "SELECT track_path, cluster_id FROM play_counts "
                f"WHERE track_path IN ({placeholders})"
            )
            async with conn.execute(sql, chunk) as cursor:
                for r in await cursor.fetchall():
                    cid = r[1]
                    out[r[0]] = int(cid) if cid is not None else None
        return out

    async def fix_and_normalize_track_genres(self) -> dict:
        """Back-fill album genres from API artist metadata — NON-DESTRUCTIVELY —
        and maintain the multi-label `genre_bucket` alongside the specific
        `genre`.

        Two columns per album:
          • `genre`        — the human DISPLAY tag. A genuinely specific existing
            tag is KEPT (a 'Trap' / 'Boom Bap' is never flattened to 'Hip-Hop').
            A value that is empty, a placeholder ('Various'/'Divers'/'Unknown'),
            or itself just a coarse bucket label (an artifact of the OLD
            collapsing normalization) is re-derived to the artist's top
            MusicBrainz tag — so prior collapses are gently undone
            ('Hip-Hop' → 'Grime').
          • `genre_bucket` — the album's coarse families as a comma-joined,
            sorted MULTI-LABEL set ('Metal,Pop,Rock/Alt'), not one winner.

        ── Why multi-label + an artist consensus ────────────────────────────────
        Source tags are comma-lists ('Rock, Metal, Pop') and the old code forced
        them through `genre_bucket`'s first-match-wins collapse, which is decided
        by the RULE priority order rather than by the music. Measured on the real
        library, that made 21% of multi-album artists straddle two families for
        no musical reason — Slipknot's `Iowa` (tagged bare 'Rock') landed in
        Rock/Alt while the rest of the discography landed in Metal, and Gojira's
        `Fortitude` ('Pop, Rock') split off from four albums tagged
        '{Metal, Pop, Rock}'. The source simply omitted a tag; nothing about the
        record changed.

        So the rule here is **omission is noise, addition is signal**:

            core(artist) = tokens carried by at least half the artist's tagged
                           albums (min 2 attesting) — their stable identity
            album_final  = core(artist) ∪ tokens(this album)

        An album that DROPS a family recovers it from the core (Iowa regains
        Metal). An album that ADDS one keeps it, because extra evidence is
        trustworthy in a way that a missing tag is not — which is precisely the
        genuine case worth preserving, a metal artist's album that really does
        carry a Jazz or Electronic tag.

        MusicBrainz artist tags are used ONLY as the fallback for albums (and
        artists) with no source tag at all, and only from a `status='ok'` row: a
        'lowconfidence' row can be an outright wrong entity (a Greek rapper
        resolved to a Japanese j-core act), and letting one re-derive the display
        overwrites correct source data with fiction.

        NB the walk reads NEITHER column (it uses artist_enrichment.genres
        directly), so this is display/grouping only.

        Returns {'scanned', 'genre_rederived', 'bucket_updated', 'consensus_repaired'}.
        """
        from utils.genre_taxonomy import (
            genre_bucket, genre_tokens, genre_display_label, GENRE_BUCKET_LABELS,
        )
        conn = await self.get_connection()

        sql = (
            "SELECT al.id AS album_id, al.genre AS current_genre, "
            "al.genre_bucket AS current_bucket, ar.name AS artist_name, "
            "e.genres AS api_genres, e.status AS api_status "
            "FROM albums al "
            "JOIN artists ar ON ar.id = al.artist_id "
            "LEFT JOIN artist_enrichment e ON e.artist_name = ar.name"
        )
        async with conn.execute(sql) as cursor:
            rows = await cursor.fetchall()

        # Display values that carry no more information than the bucket column
        # will — safe to re-derive to a finer tag. 'divers' is Qobuz's French
        # locale placeholder and was previously mistaken for a real genre, so it
        # survived normalization forever and split artists into a phantom
        # 'Other' family.
        _PLACEHOLDER = {
            "", "various", "various artists", "misc", "unknown", "other",
            "divers", "musique diverse", "special purpose artist", "autre",
        }

        def _is_placeholder(tag: str) -> bool:
            return tag.strip().lower() in _PLACEHOLDER

        def _real_tokens(tag: str) -> set:
            """Coarse families of a source tag, minus the non-informative
            sentinels. Empty set for a placeholder / unrecognised tag."""
            if not tag or _is_placeholder(tag):
                return set()
            return {t for t in genre_tokens(tag) if t not in ("Unknown", "Other")}

        from collections import Counter, defaultdict

        # ── Pass 1: per-artist token vote over that artist's own source tags ──
        by_artist: dict = defaultdict(list)
        for row in rows:
            by_artist[row["artist_name"]].append(row)

        # Two DIFFERENT artist-level summaries, for two different jobs:
        #   core  — repairs an album that HAS tags but omitted a family. This
        #           overrides what the source said, so it must be well attested:
        #           half the tagged albums, and never on a single album's word.
        #   union — fills an album with NO usable tag at all. Nothing is being
        #           overridden here (absence of a tag is absence of information),
        #           so any family the artist demonstrably carries beats leaving
        #           the album unclassified. Without this, an artist whose only
        #           tagged album is a lone 'Rap' leaves every 'Divers' sibling
        #           stranded at NULL.
        core_by_artist: dict = {}
        union_by_artist: dict = {}
        mb_by_artist: dict = {}
        for artist, arows in by_artist.items():
            album_tokens = [
                _real_tokens(r["current_genre"] or "") for r in arows
            ]
            album_tokens = [t for t in album_tokens if t]
            votes: Counter = Counter()
            for toks in album_tokens:
                votes.update(toks)
            n = len(album_tokens)
            threshold = max(2, (n + 1) // 2) if n else 0
            core_by_artist[artist] = {
                t for t, c in votes.items() if c >= threshold
            }
            union_by_artist[artist] = set(votes)

            # MusicBrainz artist tags — fallback only, and only when trusted.
            mb_toks: set = set()
            first = arows[0]
            if (first["api_status"] or "") == "ok" and first["api_genres"]:
                try:
                    for g in json.loads(first["api_genres"]):
                        if isinstance(g, dict) and g.get("name"):
                            mb_toks |= _real_tokens(g["name"])
                except Exception:
                    pass
            mb_by_artist[artist] = mb_toks

        genre_updates: list[tuple[str, int]] = []    # (display_tag, album_id)
        bucket_updates: list[tuple[str, int]] = []   # (bucket_csv, album_id)
        consensus_repaired = 0

        for row in rows:
            album_id = row["album_id"]
            artist = row["artist_name"]
            curr_genre = (row["current_genre"] or "").strip()
            curr_bucket = (row["current_bucket"] or "").strip()
            api_status = (row["api_status"] or "")
            api_genres_raw = row["api_genres"]

            # Parse API tags → weighted bucket votes + tags sorted by count desc.
            # ONLY from a trusted row; a lowconfidence match must never rewrite
            # the display.
            bucket_votes: Counter = Counter()
            tags_by_count: list[str] = []
            if api_genres_raw and api_status == "ok":
                try:
                    parsed = json.loads(api_genres_raw)
                    if isinstance(parsed, list):
                        scored = []
                        for g in parsed:
                            if isinstance(g, dict) and g.get("name"):
                                tag_name = g.get("name")
                                try:
                                    cnt = max(int(g.get("count", 1) or 1), 1)
                                except (TypeError, ValueError):
                                    cnt = 1
                                scored.append((cnt, tag_name))
                                b = genre_bucket(tag_name)
                                if b not in ("Unknown", "Other"):
                                    bucket_votes[b] += cnt
                        scored.sort(key=lambda x: -x[0])
                        tags_by_count = [t for _, t in scored]
                except Exception:
                    pass
            api_consensus = bucket_votes.most_common(1)[0][0] if bucket_votes else None

            # ── DISPLAY tag ──────────────────────────────────────────────────
            # Keep genuinely specific tags. Re-derive only placeholders and old
            # collapse-artifact bucket labels, and ONLY to a tag that belongs to
            # the consensus family — so the display can never contradict the
            # bucket or drift into a noisy off-genre top tag (a 'Psychobilly'
            # buckets to Other ≠ consensus → rejected; 'grime' buckets to the
            # Hip-Hop consensus → accepted as 'Grime').
            rederivable = (
                _is_placeholder(curr_genre)
                or curr_genre in GENRE_BUCKET_LABELS
            )
            display = curr_genre
            if rederivable:
                if api_consensus:
                    rep = next(
                        (t for t in tags_by_count if genre_bucket(t) == api_consensus),
                        None,
                    )
                    if rep:
                        display = rep.title()
                elif _is_placeholder(curr_genre) and tags_by_count:
                    # No family consensus and nothing to keep — surface the raw
                    # top tag rather than leave an empty/placeholder.
                    display = genre_display_label(tags_by_count[0])
            if display != curr_genre:
                genre_updates.append((display, album_id))

            # ── BUCKET: multi-label, repaired against the artist consensus ────
            own = _real_tokens(curr_genre) or _real_tokens(display)
            mb_tags = set(mb_by_artist.get(artist) or set())
            if own:
                # Tagged album: repair omissions from the well-attested core & enriched artist tags,
                # keep any family the album adds on its own.
                final = own | core_by_artist.get(artist, set()) | mb_tags
            else:
                # Untagged / placeholder album: inherit whatever the artist
                # demonstrably is, then trusted MusicBrainz, then the display tag.
                final = set(union_by_artist.get(artist) or set()) | mb_tags
                if not final and api_consensus:
                    final = {api_consensus}
                if not final and display:
                    b = genre_bucket(display)
                    if b not in ("Unknown", "Other"):
                        final = {b}
            if own and final > own:
                consensus_repaired += 1

            new_bucket = ",".join(sorted(final)) if final else None
            if new_bucket and new_bucket != curr_bucket:
                bucket_updates.append((new_bucket, album_id))

        async with self._write_lock:
            conn = await self.get_connection()
            if genre_updates:
                await conn.executemany(
                    "UPDATE albums SET genre = ? WHERE id = ?", genre_updates,
                )
            if bucket_updates:
                await conn.executemany(
                    "UPDATE albums SET genre_bucket = ? WHERE id = ?", bucket_updates,
                )
            if genre_updates or bucket_updates:
                await conn.commit()

        summary = {
            "scanned": len(rows),
            "genre_rederived": len(genre_updates),
            "bucket_updated": len(bucket_updates),
            "consensus_repaired": consensus_repaired,
        }
        logger.info("fix_and_normalize_track_genres: %s", summary)
        return summary


