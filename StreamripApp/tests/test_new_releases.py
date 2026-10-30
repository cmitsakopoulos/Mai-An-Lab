"""New releases by library artists (utils/new_releases.py) and the Search
landing page that shows them."""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import flet as ft

from utils import new_releases as nr
from utils.db_manager import DatabaseManager


def _days_ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


async def _library(tmpdir):
    db = DatabaseManager(os.path.join(tmpdir, "lib.db"))
    await db.initialize()
    conn = await db.get_connection()
    await conn.execute("INSERT INTO artists (id, name) VALUES (1, 'Burial'), (2, 'Mad Clip'), "
                       "(3, 'Billy Sio, Mad Clip'), (4, 'Various Artists')")
    await conn.execute("INSERT INTO albums (id, artist_id, title) VALUES "
                       "(1, 1, 'Untrue'), (2, 2, 'Ksexwrista'), (3, 3, 'Duet'), (4, 4, 'Hits')")
    for i, album in enumerate([1, 1, 2, 3, 4], 1):
        await conn.execute("INSERT INTO tracks (album_id, title, path) VALUES (?, ?, ?)",
                           (album, f"Song {i}", f"/m/{i}.flac"))
    await conn.commit()
    return db


class _FakeDeezer:
    """Stands in for ReleaseFetcher._deezer: path -> JSON (None = network error)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __call__(self, path, params=None):
        self.calls.append(path)
        return self.routes.get(path)


class TestNormalisation(unittest.TestCase):
    def test_norm_ignores_case_accents_and_edition_notes(self):
        self.assertEqual(nr.norm("Ξεχωριστά (Deluxe Edition)"), nr.norm("ξεχωριστα"))
        self.assertEqual(nr.norm("Untrue [Remastered]"), "untrue")
        self.assertEqual(nr.norm("Rossi."), "rossi")

    def test_credit_strings_and_compilations_are_not_looked_up(self):
        self.assertTrue(nr.is_lookup_candidate("Mad Clip"))
        self.assertTrue(nr.is_lookup_candidate("Simon & Garfunkel") is False)
        self.assertFalse(nr.is_lookup_candidate("Billy Sio, Mad Clip"))
        self.assertFalse(nr.is_lookup_candidate("Various Artists"))


class TestDeezerIdentity(unittest.TestCase):
    def _fetch(self, routes, name, owned, known=None):
        f = nr.ReleaseFetcher(session=None)
        f._deezer = _FakeDeezer(routes)
        return asyncio.run(f.deezer_releases(name, owned, known)), f._deezer

    def test_picks_the_namesake_whose_discography_overlaps_the_library(self):
        routes = {
            "search/artist": {"data": [{"id": 1, "name": "Burial"}, {"id": 2, "name": "BURIAL"}]},
            "artist/1/albums": {"data": [{"title": "Rot In Hell", "release_date": _days_ago(3),
                                          "record_type": "album"}]},
            "artist/2/albums": {"data": [{"title": "Untrue", "release_date": "2007-11-05"},
                                         {"title": "Dreamfear", "release_date": _days_ago(10),
                                          "record_type": "ep", "cover_medium": "c.jpg"}]},
        }
        (status, sid, releases), _ = self._fetch(routes, "Burial", {"untrue"})
        self.assertEqual((status, sid), ("ok", "2"))
        self.assertEqual([r["title"] for r in releases], ["Dreamfear"])  # 2007 is outside KEEP_DAYS
        self.assertEqual(releases[0]["cover"], "c.jpg")

    def test_no_overlap_means_not_found_rather_than_a_guess(self):
        routes = {"search/artist": {"data": [{"id": 1, "name": "LEX"}]},
                  "artist/1/albums": {"data": [{"title": "Other", "release_date": _days_ago(1)}]}}
        (status, sid, releases), _ = self._fetch(routes, "LEX", {"2xxx"})
        self.assertEqual((status, sid, releases), ("notfound", None, []))

    def test_ambiguous_name_with_nothing_owned_to_compare_is_skipped(self):
        routes = {"search/artist": {"data": [{"id": 1, "name": "Strat"}, {"id": 2, "name": "Strat"}]}}
        (status, _, _), fake = self._fetch(routes, "Strat", set())
        self.assertEqual(status, "notfound")
        self.assertEqual(fake.calls, ["search/artist"])

    def test_a_known_deezer_id_skips_the_search(self):
        routes = {"artist/9/albums": {"data": []}}
        (status, sid, _), fake = self._fetch(routes, "Burial", {"untrue"}, known="9")
        self.assertEqual((status, sid), ("ok", "9"))
        self.assertEqual(fake.calls, ["artist/9/albums"])

    def test_network_failure_is_an_error_not_a_miss(self):
        (status, _, releases), _ = self._fetch({}, "Burial", {"untrue"})
        self.assertEqual((status, releases), ("error", None))


class TestRefreshAndSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.db = self.loop.run_until_complete(_library(self.tmp.name))
        self.addCleanup(lambda: self.loop.run_until_complete(self.db.close()))

    def run_(self, coro):
        return self.loop.run_until_complete(coro)

    def _refresh(self, deezer_result, mb_result=("notfound", None, [])):
        looked_up = []

        async def deezer(self_, name, owned, known=None):
            looked_up.append(name)
            return deezer_result(name) if callable(deezer_result) else deezer_result

        async def mb(self_, mbid):
            return mb_result

        with patch.object(nr.ReleaseFetcher, "deezer_releases", deezer), \
             patch.object(nr.ReleaseFetcher, "musicbrainz_releases", mb):
            written = self.run_(nr.refresh_new_releases(self.db, session=object(), mb_client=object()))
        return written, looked_up

    def test_only_solo_artists_are_looked_up_and_then_cached_for_a_week(self):
        written, looked_up = self._refresh(("ok", "1", []))
        self.assertEqual(sorted(looked_up), ["Burial", "Mad Clip"])
        self.assertEqual(written, 2)
        written, looked_up = self._refresh(("ok", "1", []))
        self.assertEqual((written, looked_up), (0, []))

    def test_network_errors_are_not_cached(self):
        written, _ = self._refresh(("error", None, None))
        self.assertEqual(written, 0)
        self.assertEqual(self.run_(self.db.get_artist_release_cache()), {})

    def test_musicbrainz_covers_artists_deezer_cannot_identify(self):
        self.run_(self.db.upsert_artist_enrichment("Mad Clip", mbid="mb-1"))
        rel = [{"title": "Νέο", "date": _days_ago(5), "type": "album", "cover": "", "source": "musicbrainz"}]
        self._refresh(("notfound", None, []), ("ok", "mb-1", rel))
        cache = self.run_(self.db.get_artist_release_cache())
        self.assertEqual(cache["mad clip"]["source"], "musicbrainz")
        self.assertEqual(cache["burial"]["source"], "none")

    def test_selection_rules(self):
        save = lambda name, rels: self.run_(self.db.save_artist_releases(name, "deezer", "1", rels))
        save("Burial", [
            {"title": "Untrue (Deluxe)", "date": _days_ago(2), "type": "album"},     # owned
            {"title": "Dreamfear", "date": _days_ago(20), "type": "ep"},
            {"title": "Antidawn", "date": _days_ago(200), "type": "album"},
            {"title": "Old Single", "date": _days_ago(100), "type": "single"},       # stale single
            {"title": "Shock (Remix)", "date": _days_ago(1), "type": "single"},      # variant
            {"title": "Future", "date": (dt.date.today() + dt.timedelta(days=9)).isoformat(),
             "type": "album"},                                                       # unreleased
        ])
        save("Mad Clip", [
            {"title": "One", "date": _days_ago(1), "type": "single"},
            {"title": "Two", "date": _days_ago(2), "type": "single"},
            {"title": "Three", "date": _days_ago(3), "type": "single"},
        ])
        got = self.run_(nr.get_new_releases(self.db))
        self.assertEqual([(r["artist"], r["title"]) for r in got],
                         [("Mad Clip", "One"), ("Mad Clip", "Two"),
                          ("Burial", "Dreamfear"), ("Burial", "Antidawn")])


class TestLanding(unittest.TestCase):
    def test_sections_run_top_to_bottom_with_stats_last(self):
        from ui.views.search import SearchView
        v = SearchView.__new__(SearchView)
        v.app = MagicMock()
        v.app._is_restarting = False
        v.app.new_releases_refreshing = False
        db = v.app.db_manager

        async def rows(*a, **k):
            return [{"title": "T", "artist": "A", "count": 3, "path": "/x",
                     "name": "N", "plays": 2, "tracks_played": 1, "albums_played": 1}]

        async def zero(*a, **k):
            return 0

        async def empty(*a, **k):
            return []
        db.get_most_played = rows
        db.get_most_played_artists = rows
        db.get_most_played_genres = rows
        db.get_total_tracks = zero
        db.get_all_artists = db.get_all_albums = db.get_all_playlists = empty
        v._landing_container = ft.ListView()
        release = [{"title": "Dreamfear", "artist": "Burial", "date": _days_ago(3),
                    "type": "ep", "cover": ""}]

        async def releases(*a, **k):
            return release
        with patch("utils.new_releases.get_new_releases", releases), \
             patch("utils.search_history.load_searches", lambda: ["burial", "mad clip"]):
            asyncio.run(v._refresh_landing_page({k: True for k in (
                "recent_searches", "new_releases", "most_listened", "top_artists",
                "top_genres", "stats")}))

        def title(card):
            col = card.content
            first = col.controls[0]
            return first.value if isinstance(first, ft.Text) else first.controls[1].value
        self.assertEqual([title(c) for c in v._landing_container.controls], [
            "Recent searches", "New From Your Artists", "Most Listened Tracks",
            "Most Listened Artists", "Most Listened Genres", "Library at a Glance",
        ])

    def test_release_dates_read_naturally(self):
        from ui.views.search import _release_date_label
        self.assertEqual(_release_date_label(dt.date.today().isoformat()), "Today")
        self.assertEqual(_release_date_label(_days_ago(1)), "Yesterday")
        self.assertEqual(_release_date_label(_days_ago(3)), "3 days ago")
        self.assertEqual(_release_date_label("2019-03-07"), "7 Mar 2019")


if __name__ == "__main__":
    unittest.main()
