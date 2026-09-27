"""Tests for the Genre View implementation across DatabaseManager, LibraryView,
and Metadata Workbench routing.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft
from utils.db_manager import DatabaseManager
from ui.views.library import LibraryView, _genre_color
from ui.tokens import LIB_GENRE_COLOR


def _run(coro):
    return asyncio.run(coro)


class TestDatabaseManagerGenreMethods(unittest.TestCase):
    """Test the SQLite recursive CTE genre aggregation, multi-label parsing,
    search, and scoped queries in DatabaseManager."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = DatabaseManager(self.tmp.name)
        _run(self.db.initialize())

    def tearDown(self):
        _run(self.db.close())
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    async def _seed_data(self):
        conn = await self.db.get_connection()
        # Artist 1: Radiohead
        a1 = await self.db._get_or_create_artist(conn, "Radiohead")
        # Artist 2: Portishead
        a2 = await self.db._get_or_create_artist(conn, "Portishead")
        # Artist 3: Miles Davis
        a3 = await self.db._get_or_create_artist(conn, "Miles Davis")

        # Albums
        # Radiohead: OK Computer (Alt, Rock) - 10 base + 2 inserted tracks = 12 tracks
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre, genre_bucket, track_count) "
            "VALUES (1, ?, 'OK Computer', 1997, 'Alternative Rock', 'Alt,Rock', 10)",
            (a1,),
        )
        # Radiohead: Kid A (Electronic, Alt) - 10 tracks
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre, genre_bucket, track_count) "
            "VALUES (2, ?, 'Kid A', 2000, 'Electronic', 'Electronic,Alt', 10)",
            (a1,),
        )
        # Portishead: Dummy (Electronic, Trip Hop) - 10 base + 1 inserted track = 11 tracks
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre, genre_bucket, track_count) "
            "VALUES (3, ?, 'Dummy', 1994, 'Trip Hop', 'Electronic,Trip Hop', 10)",
            (a2,),
        )
        # Miles Davis: Kind of Blue (Jazz) - 4 base + 1 inserted track = 5 tracks
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre, genre_bucket, track_count) "
            "VALUES (4, ?, 'Kind of Blue', 1959, 'Modal Jazz', 'Jazz', 4)",
            (a3,),
        )
        # Album with placeholder genre to verify exclusion
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre, genre_bucket, track_count) "
            "VALUES (5, ?, 'Unknown Album', 2020, 'unknown', 'unknown', 3)",
            (a3,),
        )

        # Tracks
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, track_num, path, duration, added_date) "
            "VALUES (1, 1, 'Airbag', 1, '/music/radiohead/airbag.flac', 284, 1000)"
        )
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, track_num, path, duration, added_date) "
            "VALUES (2, 1, 'Paranoid Android', 2, '/music/radiohead/paranoid.flac', 383, 1001)"
        )
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, track_num, path, duration, added_date) "
            "VALUES (3, 3, 'Mysterons', 1, '/music/portishead/mysterons.flac', 302, 1002)"
        )
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, track_num, path, duration, added_date) "
            "VALUES (4, 4, 'So What', 1, '/music/miles/sowhat.flac', 562, 1003)"
        )
        await conn.commit()

    def test_get_all_genres_aggregation(self):
        _run(self._seed_data())
        genres = _run(self.db.get_all_genres(sort_mode="name"))
        genre_map = {g["name"]: g for g in genres}

        # Check multi-label splitting worked
        self.assertIn("Alt", genre_map)
        self.assertIn("Rock", genre_map)
        self.assertIn("Electronic", genre_map)
        self.assertIn("Trip Hop", genre_map)
        self.assertIn("Jazz", genre_map)

        # "unknown" should be filtered out
        self.assertNotIn("unknown", genre_map)
        self.assertNotIn("Other", genre_map)

        # Alt: 1 artist (Radiohead), 2 albums (OK Computer, Kid A), 22 tracks
        self.assertEqual(genre_map["Alt"]["artist_count"], 1)
        self.assertEqual(genre_map["Alt"]["album_count"], 2)
        self.assertEqual(genre_map["Alt"]["track_count"], 22)

        # Electronic: 2 artists (Radiohead, Portishead), 2 albums (Kid A, Dummy), 21 tracks
        self.assertEqual(genre_map["Electronic"]["artist_count"], 2)
        self.assertEqual(genre_map["Electronic"]["album_count"], 2)
        self.assertEqual(genre_map["Electronic"]["track_count"], 21)

        # Jazz: 1 artist (Miles Davis), 1 album, 5 tracks
        self.assertEqual(genre_map["Jazz"]["artist_count"], 1)
        self.assertEqual(genre_map["Jazz"]["album_count"], 1)
        self.assertEqual(genre_map["Jazz"]["track_count"], 5)

    def test_get_all_genres_sorting(self):
        _run(self._seed_data())

        # Sort by tracks descending
        by_tracks = _run(self.db.get_all_genres(sort_mode="tracks"))
        track_counts = [g["track_count"] for g in by_tracks]
        self.assertEqual(track_counts, sorted(track_counts, reverse=True))

        # Sort by artists descending
        by_artists = _run(self.db.get_all_genres(sort_mode="artists"))
        artist_counts = [g["artist_count"] for g in by_artists]
        self.assertEqual(artist_counts, sorted(artist_counts, reverse=True))
        self.assertEqual(by_artists[0]["name"], "Electronic")  # 2 artists

    def test_get_all_genres_search(self):
        _run(self._seed_data())

        # Exact substring search
        res = _run(self.db.get_all_genres(search_query="electr"))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["name"], "Electronic")

        # Fuzzy k-mer fallback
        res_fuzzy = _run(self.db.get_all_genres(search_query="electonic"))  # typo
        self.assertTrue(getattr(res_fuzzy, "is_closest", False))
        self.assertGreater(len(res_fuzzy), 0)
        self.assertEqual(res_fuzzy[0]["name"], "Electronic")

    def test_get_artists_by_genre(self):
        _run(self._seed_data())

        # Artists for Electronic
        artists = _run(self.db.get_artists_by_genre("Electronic"))
        names = [a["name"] for a in artists]
        self.assertIn("Radiohead", names)
        self.assertIn("Portishead", names)
        self.assertNotIn("Miles Davis", names)

        # Artists for Jazz
        jazz_artists = _run(self.db.get_artists_by_genre("Jazz"))
        self.assertEqual(len(jazz_artists), 1)
        self.assertEqual(jazz_artists[0]["name"], "Miles Davis")

    def test_get_albums_by_artist_scoped_by_genre(self):
        _run(self._seed_data())

        # All albums for Radiohead
        all_albums = _run(self.db.get_albums_by_artist("Radiohead"))
        self.assertEqual(len(all_albums), 2)

        # Filtered by Rock (only OK Computer has Rock)
        rock_albums = _run(self.db.get_albums_by_artist("Radiohead", genre="Rock"))
        self.assertEqual(len(rock_albums), 1)
        self.assertEqual(rock_albums[0]["album"], "OK Computer")

        # Filtered by Electronic (only Kid A has Electronic)
        elec_albums = _run(self.db.get_albums_by_artist("Radiohead", genre="Electronic"))
        self.assertEqual(len(elec_albums), 1)
        self.assertEqual(elec_albums[0]["album"], "Kid A")

    def test_get_tracks_by_genre(self):
        _run(self._seed_data())

        tracks = _run(self.db.get_tracks_by_genre("Rock"))
        paths = [t["path"] for t in tracks]
        self.assertIn("/music/radiohead/airbag.flac", paths)
        self.assertIn("/music/radiohead/paranoid.flac", paths)
        self.assertNotIn("/music/portishead/mysterons.flac", paths)


class TestLibraryViewGenreUI(unittest.TestCase):
    """Test LibraryView genre row generation, chevron state, hierarchy indentation,
    and expansion mechanics."""

    def _make_view(self):
        view = LibraryView.__new__(LibraryView)
        view.app = MagicMock()
        view.app.safe_update.side_effect = lambda fn: fn()
        view.page = MagicMock()
        view.expanded_nodes = set()
        view.selected_genre = None
        view.view_mode = "genres"
        view.search_query = ""
        view.sort_mode = "name"
        view._toggling_nodes = set()
        view._library_list = ft.ListView()
        view._search_field = ft.TextField()
        view._tabs_row = ft.Row()
        return view

    def test_genre_row_construction(self):
        view = self._make_view()
        genre_data = {
            "name": "Alternative",
            "artist_count": 5,
            "album_count": 12,
            "track_count": 85,
        }
        row = view._genre_row(genre_data, "genre_Alternative", False)

        self.assertIsInstance(row, ft.ListTile)
        self.assertEqual(row.data["node_id"], "genre_Alternative")
        self.assertEqual(row.data["depth"], 0)
        self.assertEqual(row.data["type"], "genre")
        self.assertEqual(row.data["name"], "Alternative")

        # Title & Subtitle
        self.assertEqual(row.title.value, "Alternative")
        self.assertIn("5 artists", row.subtitle.value)
        self.assertIn("12 albums", row.subtitle.value)
        self.assertIn("85 tracks", row.subtitle.value)

        # Trailing controls: Play, Curate, Chevron
        trailing = row.trailing
        self.assertIsInstance(trailing, ft.Row)
        self.assertEqual(len(trailing.controls), 3)

        play_btn = trailing.controls[0]
        curate_btn = trailing.controls[1]
        chevron = trailing.controls[2]

        self.assertEqual(play_btn.icon, ft.Icons.PLAY_ARROW_ROUNDED)
        self.assertEqual(curate_btn.icon, ft.Icons.TUNE_ROUNDED)
        self.assertEqual(chevron.icon, ft.Icons.CHEVRON_RIGHT_ROUNDED)
        self.assertEqual(chevron.rotate.angle, 0)

        # Expanded state
        expanded_row = view._genre_row(genre_data, "genre_Alternative", True)
        exp_chevron = expanded_row.trailing.controls[2]
        self.assertAlmostEqual(exp_chevron.rotate.angle, 1.57, places=2)

    def test_artist_row_with_genre_context_and_depth(self):
        view = self._make_view()
        artist_data = {
            "name": "Massive Attack",
            "album_count": 5,
            "track_count": 48,
        }
        # Nested artist under genre (depth=1, genre_context="Trip Hop")
        row = view._artist_row(
            artist_data,
            "genre_Trip Hop_artist_Massive Attack",
            False,
            depth=1,
            genre_context="Trip Hop",
        )
        self.assertEqual(row.data["depth"], 1)
        self.assertEqual(row.data["genre_context"], "Trip Hop")
        self.assertEqual(row.data["name"], "Massive Attack")

        # Indentation check
        leading = row.leading
        self.assertIsInstance(leading, ft.Row)
        indent_container = leading.controls[0]
        self.assertEqual(indent_container.width, 16)  # depth * 16

    def test_toggle_node_expansion_genre_to_artists(self):
        view = self._make_view()
        view._library_list.controls = []

        genre_tile = view._genre_row({"name": "Rock", "artist_count": 2, "album_count": 3, "track_count": 30}, "genre_Rock", False)
        view._library_list.controls.append(genre_tile)

        # Mock DB response
        mock_artists = [
            {"id": 1, "name": "Queen", "album_count": 2, "track_count": 20},
            {"id": 2, "name": "Nirvana", "album_count": 1, "track_count": 10},
        ]
        view.app.db_manager.get_artists_by_genre = AsyncMock(return_value=mock_artists)

        # Toggle open
        _run(view._toggle_node("genre_Rock", genre_tile))

        view.app.db_manager.get_artists_by_genre.assert_awaited_once_with("Rock")
        self.assertIn("genre_Rock", view.expanded_nodes)

        # Controls should now have genre_tile + 2 artist tiles
        self.assertEqual(len(view._library_list.controls), 3)
        a1 = view._library_list.controls[1]
        a2 = view._library_list.controls[2]
        self.assertEqual(a1.data["type"], "artist")
        self.assertEqual(a1.data["name"], "Queen")
        self.assertEqual(a1.data["depth"], 1)
        self.assertEqual(a1.data["genre_context"], "Rock")

        self.assertEqual(a2.data["type"], "artist")
        self.assertEqual(a2.data["name"], "Nirvana")
        self.assertEqual(a2.data["depth"], 1)
        self.assertEqual(a2.data["genre_context"], "Rock")

    def test_toggle_node_expansion_artist_under_genre_to_albums(self):
        view = self._make_view()
        view.expanded_nodes = {"genre_Rock"}
        view._library_list.controls = []

        genre_tile = view._genre_row({"name": "Rock", "artist_count": 1, "album_count": 1, "track_count": 10}, "genre_Rock", True)
        artist_tile = view._artist_row({"name": "Queen", "album_count": 1, "track_count": 10}, "genre_Rock_artist_Queen", False, depth=1, genre_context="Rock")
        view._library_list.controls.extend([genre_tile, artist_tile])

        # Mock DB response for get_albums_by_artist with genre filter
        mock_albums = [
            {"id": 1, "album": "A Night at the Opera", "artist": "Queen", "year": 1975, "genre": "Rock", "track_count": 12},
        ]
        view.app.db_manager.get_albums_by_artist = AsyncMock(return_value=mock_albums)

        # Toggle open Queen
        _run(view._toggle_node("genre_Rock_artist_Queen", artist_tile))

        view.app.db_manager.get_albums_by_artist.assert_awaited_once_with("Queen", genre="Rock")
        self.assertIn("genre_Rock_artist_Queen", view.expanded_nodes)
        self.assertEqual(len(view._library_list.controls), 3)
        alb = view._library_list.controls[2]
        self.assertEqual(alb.data["type"], "album")
        self.assertEqual(alb.data["depth"], 2)


class TestGenreRoutingAndWorkbench(unittest.TestCase):
    """Test genre Quick Play routing, Workbench filtering, and Settings integration."""

    def test_workbench_filter_genre(self):
        from ui.player.metadata_workbench import MetadataWorkbenchPane

        pane = MetadataWorkbenchPane.__new__(MetadataWorkbenchPane)
        pane._search_field = ft.TextField()
        pane._reload = MagicMock()

        pane.filter_genre("Synthwave")

        self.assertEqual(pane.filter, "all")
        self.assertEqual(pane.search, "Synthwave")
        self.assertEqual(pane._search_field.value, "Synthwave")
        pane._reload.assert_called_once()

    def test_open_genre_metadata_workbench(self):
        from main import StreamripFletApp

        app = StreamripFletApp.__new__(StreamripFletApp)
        app.switch_tab = MagicMock()
        app.settings_view = MagicMock()

        app.open_genre_metadata_workbench("Indie")

        app.switch_tab.assert_called_once_with(3)
        app.settings_view._on_open_metadata_workbench_click.assert_called_once_with(genre_filter="Indie")


if __name__ == "__main__":
    unittest.main()
