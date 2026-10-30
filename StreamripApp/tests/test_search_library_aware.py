"""Library-aware Search: what the user already owns is shown next to (and,
when the remote search fails, instead of) the Qobuz/Deezer results."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from ui.views.search import SearchView
from utils.db_manager import DatabaseManager


async def _library(tmpdir):
    """Burial: Untrue (3 of its 13 tracks owned). Boards of Canada: one track."""
    db = DatabaseManager(os.path.join(tmpdir, "lib.db"))
    await db.initialize()
    conn = await db.get_connection()
    await conn.execute("INSERT INTO artists (id, name) VALUES (1, 'Burial'), (2, 'Boards of Canada')")
    await conn.execute("INSERT INTO albums (id, artist_id, title, year) VALUES "
                       "(1, 1, 'Untrue', '2007'), (2, 2, 'Geogaddi', '2002')")
    for i, (album, title) in enumerate([(1, "Archangel"), (1, "Near Dark"),
                                        (1, "Ghost Hardware"), (2, "Music Is Math")], 1):
        await conn.execute(
            "INSERT INTO tracks (album_id, title, path, added_date) VALUES (?, ?, ?, ?)",
            (album, title, f"/music/{i}.flac", i))
    await conn.commit()
    return db


def _view(db):
    v = SearchView.__new__(SearchView)
    v.app = MagicMock()
    v.app.db_manager = db
    v.page = MagicMock()
    v.selected_source = "qobuz"
    v.view_mode = "tracks"
    v.current_page = 0
    v.total_pages = 1
    v.items_per_page = 35
    v.expanded_nodes = set()
    v.selection_mode = False
    v.selected_keys = set()
    v._selected_records = {}
    v._source_cache = {}
    v._cache_query = {}
    v._local_matches = {}
    v._remote_error = None
    v._search_field = ft.TextField(value="burial")
    v._results_list = ft.ListView()
    for name in ("_setup_prompt", "_empty_label", "_error_label", "_landing_container",
                 "_pagination_bar"):
        setattr(v, name, ft.Container())
    v._error_detail = ft.Text()
    v._page_label = ft.Text()
    v._prev_page_btn = ft.IconButton()
    v._next_page_btn = ft.IconButton()
    return v


def _texts(control) -> list[str]:
    """Every Text value in a control tree."""
    out = []

    def walk(c):
        if isinstance(c, ft.Text) and c.value:
            out.append(c.value)
        for attr in ("content", "controls", "title", "subtitle", "leading", "trailing"):
            child = getattr(c, attr, None)
            if isinstance(child, list):
                for x in child:
                    walk(x)
            elif isinstance(child, ft.Control):
                walk(child)
    walk(control)
    return out


class TestLibraryAwareSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.db = self.loop.run_until_complete(_library(self.tmp.name))
        self.addCleanup(lambda: self.loop.run_until_complete(self.db.close()))
        engine = patch("ui.views.search.audio_engine").start()
        engine.current_track = ""
        engine.current_artist = ""
        self.addCleanup(patch.stopall)

    def run_(self, coro):
        return self.loop.run_until_complete(coro)

    def test_album_ownership_counts_owned_tracks(self):
        v = _view(self.db)
        partial = {"media_type": "album", "ui_title": "Untrue", "ui_subtitle": "Burial", "num_tracks": 13}
        complete = {"media_type": "album", "ui_title": "Untrue", "ui_subtitle": "burial", "num_tracks": 3}
        absent = {"media_type": "album", "ui_title": "Kindred", "ui_subtitle": "Burial", "num_tracks": 3}
        for r in (partial, complete, absent):
            self.run_(v._annotate_ownership(r))
        self.assertEqual((partial["owned_tracks"], partial["is_in_library"]), (3, False))
        self.assertEqual((complete["owned_tracks"], complete["is_in_library"]), (3, True))
        self.assertEqual((absent["owned_tracks"], absent["is_in_library"]), (0, False))
        self.assertEqual(_texts(v._ownership_badge(partial)), ["YOU HAVE 3/13"])
        self.assertEqual(_texts(v._ownership_badge(complete)), ["IN LIBRARY"])
        self.assertIsNone(v._ownership_badge(absent))

    def test_artist_and_track_ownership(self):
        v = _view(self.db)
        artist = {"media_type": "artist", "ui_title": "Burial", "ui_subtitle": "12 Albums"}
        track = {"media_type": "track", "ui_title": "Archangel", "ui_subtitle": "Burial"}
        self.run_(v._annotate_ownership(artist))
        self.run_(v._annotate_ownership(track))
        self.assertEqual(_texts(v._ownership_badge(artist)), ["3 TRACKS OWNED"])
        self.assertTrue(track["is_in_library"])
        self.assertEqual(_texts(v._ownership_badge(track)), ["IN LIBRARY"])

    def test_local_matches_skip_the_closest_match_fallback(self):
        v = _view(self.db)
        hit = self.run_(v._load_local_matches("burial"))
        self.assertEqual(len(hit["track"]), 3)
        self.assertEqual([a["name"] for a in hit["artist"]], ["Burial"])
        # "burrial" finds nothing exact; the Library's fuzzy fallback would
        # still offer Burial, which Search must not present as a match.
        miss = self.run_(v._load_local_matches("zzzz unrelated"))
        self.assertEqual(miss, {"track": [], "album": [], "artist": []})

    def test_library_section_tops_page_one_only(self):
        v = _view(self.db)
        v._local_matches = self.run_(v._load_local_matches("burial"))
        v.cached_results = {"track": [{"id": str(i), "media_type": "track", "ui_title": f"T{i}",
                                       "ui_subtitle": "X"} for i in range(40)],
                            "album": [], "artist": []}
        first = v._page_controls()
        self.assertIn("In your library", _texts(first[0]))
        v.current_page = 1
        self.assertNotIn("In your library", _texts(v._page_controls()[0]))

    def test_section_shows_three_rows_and_a_see_all_link(self):
        v = _view(self.db)
        v._local_matches = {"track": [{"title": f"T{i}", "artist": "A", "path": f"/{i}"}
                                      for i in range(5)], "album": [], "artist": []}
        texts = _texts(v._build_local_section())
        self.assertEqual([t for t in texts if t.startswith("T")], ["T0", "T1", "T2"])
        self.assertIn("See all 5 in Library", texts)

    def test_remote_failure_keeps_library_matches_visible(self):
        v = _view(self.db)
        v._local_matches = self.run_(v._load_local_matches("burial"))
        v._show_search_error("Qobuz authentication failed.")
        self.assertFalse(v._error_label.visible)
        texts = _texts(v._results_list)
        self.assertIn("Couldn't search Qobuz", texts)
        self.assertIn("In your library", texts)
        # Switching type keeps the banner rather than claiming "no results".
        v.view_mode = "artists"
        texts = _texts(ft.Column(v._page_controls()))
        self.assertIn("Couldn't search Qobuz", texts)
        self.assertNotIn("No Qobuz results for this search.", texts)

    def test_remote_failure_without_library_matches_uses_the_full_error(self):
        v = _view(self.db)
        v._show_search_error("boom")
        self.assertTrue(v._error_label.visible)
        self.assertEqual(v._results_list.controls, [])

    def test_empty_remote_with_library_matches_says_so(self):
        v = _view(self.db)
        v._local_matches = self.run_(v._load_local_matches("burial"))
        v.cached_results = {"track": [], "album": [], "artist": []}
        texts = _texts(ft.Column(v._page_controls()))
        self.assertIn("In your library", texts)
        self.assertIn("No Qobuz results for this search.", texts)


class TestAlbumTrackCount(unittest.TestCase):
    def test_parsed_albums_carry_their_track_count(self):
        from utils.streamrip_search import StreamripSearcher
        s = StreamripSearcher.__new__(StreamripSearcher)
        out = s._parse_results([{"_media_type": "album", "id": 7, "title": "Untrue",
                                 "artist": {"name": "Burial"}, "tracks_count": 13}], "qobuz")
        self.assertEqual(out[0]["num_tracks"], 13)


if __name__ == "__main__":
    unittest.main()
