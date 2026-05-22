import aiosqlite
import os
import asyncio
import logging
import re
import numpy as np

logger = logging.getLogger(__name__)

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
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        artist_id   INTEGER NOT NULL,
                        title       TEXT    NOT NULL COLLATE NOCASE,
                        year        TEXT,
                        genre       TEXT,
                        track_count INTEGER DEFAULT 0,
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
                        brightness  REAL DEFAULT 0
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
                        PRIMARY KEY (mood, feature)
                    )
                ''')

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
            return [dict(r) for r in rows]

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
            return [dict(r) for r in rows]

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
            return [dict(r) for r in rows]

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
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS track_partitions (
                track_path TEXT PRIMARY KEY REFERENCES tracks(path) ON DELETE CASCADE,
                mood       TEXT,
                islet_id   INTEGER
            )
        ''')
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

    async def get_saved_partitions(self) -> dict[str, dict]:
        """Lock-free read. Returns a dictionary mapping track_path -> {mood, islet_id}."""
        conn = await self.get_connection()
        async with conn.execute("SELECT track_path, mood, islet_id FROM track_partitions") as cursor:
            rows = await cursor.fetchall()
            return {r["track_path"]: {"mood": r["mood"], "islet_id": r["islet_id"]} for r in rows}

    async def save_partitions(self, assignments: list[tuple[str, str, int]]):
        """Mutation: Overwrites the partition assignments in the database."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("DELETE FROM track_partitions")
                if assignments:
                    await conn.executemany(
                        "INSERT INTO track_partitions (track_path, mood, islet_id) VALUES (?, ?, ?)",
                        assignments
                    )
                await conn.commit()
            except Exception as e:
                await conn.rollback()
                logger.error(f"Failed to save partitions: {e}")
                raise

    async def save_mood_feedback(self, track_path: str, mood: str, feedback: int):
        """Mutation: Save like/dislike feedback for a track in a specific mood."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute(
                    "INSERT OR REPLACE INTO mood_feedback (track_path, mood, feedback) VALUES (?, ?, ?)",
                    (track_path, mood, feedback)
                )
                await conn.commit()
            except Exception as e:
                await conn.rollback()
                logger.error(f"Failed to save mood feedback: {e}")
                raise

    async def get_mood_feedback(self) -> dict[str, dict[str, int]]:
        """Lock-free read. Returns all mood feedback as a nested dict mapping track_path -> {mood: feedback}."""
        conn = await self.get_connection()
        async with conn.execute("SELECT track_path, mood, feedback FROM mood_feedback") as cursor:
            rows = await cursor.fetchall()
            out = {}
            for r in rows:
                path = r["track_path"]
                mood = r["mood"]
                val = r["feedback"]
                if path not in out:
                    out[path] = {}
                out[path][mood] = val
            return out

    async def clear_all_mood_feedback(self):
        """Mutation: Clears all mood feedback."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("DELETE FROM mood_feedback")
                await conn.commit()
            except Exception as e:
                await conn.rollback()
                logger.error(f"Failed to clear mood feedback: {e}")
                raise

    async def save_adjusted_mood_profile(self, mood: str, profile: dict[str, float]):
        """Mutation: Saves customized targets for a mood's features."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("DELETE FROM mood_profiles WHERE mood = ?", (mood,))
                if profile:
                    await conn.executemany(
                        "INSERT INTO mood_profiles (mood, feature, target) VALUES (?, ?, ?)",
                        [(mood, feat, target) for feat, target in profile.items()]
                    )
                await conn.commit()
            except Exception as e:
                await conn.rollback()
                logger.error(f"Failed to save adjusted mood profile: {e}")
                raise

    async def get_adjusted_mood_profile(self, mood: str) -> dict[str, float] | None:
        """Lock-free read. Returns customized features for a mood, or None if not customized."""
        conn = await self.get_connection()
        async with conn.execute("SELECT feature, target FROM mood_profiles WHERE mood = ?", (mood,)) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                return None
            return {r["feature"]: r["target"] for r in rows}

    async def get_all_adjusted_mood_profiles(self) -> dict[str, dict[str, float]]:
        """Lock-free read. Returns all customized mood profiles."""
        conn = await self.get_connection()
        async with conn.execute("SELECT mood, feature, target FROM mood_profiles") as cursor:
            rows = await cursor.fetchall()
            out = {}
            for r in rows:
                m = r["mood"]
                f = r["feature"]
                t = r["target"]
                if m not in out:
                    out[m] = {}
                out[m][f] = t
            return out

    async def clear_all_adjusted_mood_profiles(self):
        """Mutation: Clears all customized mood profiles."""
        async with self._write_lock:
            conn = await self.get_connection()
            try:
                await conn.execute("DELETE FROM mood_profiles")
                await conn.commit()
            except Exception as e:
                await conn.rollback()
                logger.error(f"Failed to clear adjusted mood profiles: {e}")
                raise


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
            return [dict(r) for r in rows]

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
            LEFT JOIN tracks  t  ON t.path       = pc.track_path
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
                   al.title AS album
            FROM play_counts pc
            LEFT JOIN tracks  t  ON t.path  = pc.track_path
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

    async def count_neighbors(self, edge_kind: str | None = None) -> int:
        """Total edge count, optionally filtered by kind. Used to detect
        whether the graph has been built yet."""
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
                   t.title, ar.name AS artist, al.title AS album
            FROM track_neighbors n
            LEFT JOIN tracks  t  ON t.path     = n.neighbor_path
            LEFT JOIN albums  al ON al.id      = t.album_id
            LEFT JOIN artists ar ON ar.id      = al.artist_id
            WHERE n.track_path = ? AND n.edge_kind IN ({placeholders})
            ORDER BY n.weight DESC
            LIMIT ?
        '''
        params = (track_path, *edge_kinds, k)
        async with conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_embeddings_for_paths(
        self, paths: list[str]
    ) -> dict[str, bytes]:
        """Bulk-load timbre BLOBs for an arbitrary path list. Used by the
        walk's MMR diversity term to compute candidate-to-visited similarity
        without N round-trips. Missing rows are simply absent from the map."""
        if not paths:
            return {}
        conn = await self.get_connection()
        # SQLite parameter limit is ~999; chunk to be safe at large libraries.
        out: dict[str, bytes] = {}
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            sql = (
                "SELECT track_path, timbre FROM play_counts "
                f"WHERE track_path IN ({placeholders}) AND timbre IS NOT NULL"
            )
            async with conn.execute(sql, chunk) as cursor:
                for r in await cursor.fetchall():
                    out[r[0]] = r[1]
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

    async def listen_signal_map(self) -> dict[str, float]:
        """Returns {path: normalised_signal} in roughly [-1, 1] computed as
            (completed - skipped_early) / max(plays, 5)
        Plays are denominator-floored at 5 so a single skip on a brand-new
        track only pushes its signal to -0.2 rather than -1.0. Tracks with
        no history are simply absent from the map (the consumer treats
        absence as zero)."""
        conn = await self.get_connection()
        sql = '''
            SELECT track_path,
                   SUM(CASE WHEN event = 'completed'     THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN event = 'skipped_early' THEN 1 ELSE 0 END) AS skip,
                   SUM(CASE WHEN event = 'played'        THEN 1 ELSE 0 END) AS plays
            FROM playback_history
            GROUP BY track_path
        '''
        out: dict[str, float] = {}
        async with conn.execute(sql) as cursor:
            for r in await cursor.fetchall():
                denom = max(int(r["plays"] or 0), 5)
                out[r["track_path"]] = (int(r["done"] or 0) - int(r["skip"] or 0)) / float(denom)
        return out

    async def get_track_full(self, path: str) -> dict | None:
        """Single-row lookup returning title/artist/album/genre and the DSP
        timbre BLOB for a path. The LEFT JOIN on play_counts means callers
        get `row["timbre"]` for free without a second query — used by
        islet creation (Jarvis + Library dialog) where we need the timbre
        vector immediately after grabbing the metadata."""
        conn = await self.get_connection()
        sql = '''
            SELECT t.path, t.title, t.duration, t.format,
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