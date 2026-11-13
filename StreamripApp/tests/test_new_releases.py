"""New releases by library artists (utils/new_releases.py) and the Search
landing page that shows them."""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import time
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

        async def counts(*a, **k):
            return {"tracks": 0, "albums": 0, "artists": 0, "playlists": 0}
        db.get_most_played = rows
        db.get_most_played_artists = rows
        db.get_most_played_genres = rows
        db.get_library_counts = counts
        v._landing_container = ft.ListView()
        release = [{"title": "Dreamfear", "artist": "Burial", "date": _days_ago(3),
                    "type": "ep", "cover": ""}]

        async def releases(*a, **k):
            return release
        with patch("utils.new_releases.get_new_releases", releases), \
             patch("utils.search_history.load_searches", lambda: ["burial", "mad clip"]):
            asyncio.run(v._refresh_landing_page({k: True for k in (
                "new_releases", "most_listened", "top_artists",
                "top_genres", "stats")}))

        def title(card):
            col = card.content
            first = col.controls[0]
            return first.value if isinstance(first, ft.Text) else first.controls[1].value
        self.assertEqual([title(c) for c in v._landing_container.controls], [
            "New From Your Artists", "Most Listened Tracks",
            "Most Listened Artists", "Most Listened Genres", "Library at a Glance",
        ])

    def test_release_dates_read_naturally(self):
        from ui.views.search import _release_date_label
        self.assertEqual(_release_date_label(dt.date.today().isoformat()), "Today")
        self.assertEqual(_release_date_label(_days_ago(1)), "Yesterday")
        self.assertEqual(_release_date_label(_days_ago(3)), "3 days ago")
        self.assertEqual(_release_date_label("2019-03-07"), "7 Mar 2019")

    def test_new_releases_shelf_has_navigation_controls_and_safe_height(self):
        from ui.views.search import SearchView
        v = SearchView.__new__(SearchView)
        v.app = MagicMock()
        v.page = MagicMock()
        v.app.new_releases_refreshing = False

        fake_releases = [
            {"title": f"Album {i}", "artist": "Artist", "date": _days_ago(i),
             "type": "album", "cover": ""}
            for i in range(5)
        ]
        async def _mock_releases(*a, **k):
            return fake_releases

        with patch("utils.new_releases.get_new_releases", _mock_releases):
            card = asyncio.run(v._build_new_releases_card())

        self.assertIsNotNone(card)
        col = card.content.controls[2]  # content column under the divider
        shelf_lv = None
        header_row = None
        for ctrl in col.controls:
            if isinstance(ctrl, ft.ListView):
                shelf_lv = ctrl
            elif isinstance(ctrl, ft.Row) and len(ctrl.controls) >= 3:
                header_row = ctrl

        self.assertIsNotNone(shelf_lv)
        self.assertEqual(shelf_lv.height, 196)
        self.assertFalse(shelf_lv.build_controls_on_demand)

        # Header has nav chevrons
        self.assertIsNotNone(header_row)
        nav_box = header_row.controls[2]
        self.assertTrue(nav_box.visible)
        prev_btn, next_btn = nav_box.controls[0], nav_box.controls[1]
        self.assertEqual(prev_btn.icon, ft.Icons.CHEVRON_LEFT_ROUNDED)
        self.assertEqual(next_btn.icon, ft.Icons.CHEVRON_RIGHT_ROUNDED)

        # Scrolling triggers run_task safely
        prev_btn.on_click(None)
        self.assertEqual(v.app.page.run_task.call_count, 1)

    def test_library_stats_grid_and_deep_links(self):
        from ui.views.search import SearchView
        v = SearchView.__new__(SearchView)
        v.app = MagicMock()
        db = v.app.db_manager

        async def _counts():
            return {"tracks": 14820, "albums": 85, "artists": 420, "playlists": 12}

        db.get_library_counts = _counts

        v._open_in_library = MagicMock()

        stats_content = asyncio.run(v._get_library_stats_content())
        self.assertIsInstance(stats_content, ft.Column)
        self.assertEqual(len(stats_content.controls), 2)  # 2 rows

        row1 = stats_content.controls[0]
        row2 = stats_content.controls[1]
        self.assertEqual(len(row1.controls), 2)
        self.assertEqual(len(row2.controls), 2)

        # Verify formatted numbers
        tracks_card = row1.controls[0]
        # Text control inside card's column
        val_text = tracks_card.content.controls[2]
        self.assertEqual(val_text.value, "14,820")

        # Verify deep link callback
        tracks_card.on_click(None)
        v._open_in_library.assert_called_with("tracks", "")

        artists_card = row2.controls[0]
        artists_card.on_click(None)
        v._open_in_library.assert_called_with("artists", "")

    def test_landing_page_does_not_retrigger_when_not_stale(self):
        from ui.views.search import SearchView
        v = SearchView.__new__(SearchView)
        v.app = MagicMock()
        v.page = MagicMock()
        v._search_field = ft.TextField(value="")
        v._landing_container = ft.ListView(controls=[ft.Container()])
        v._landing_stale = False
        v._landing_built_at = time.monotonic()
        v._configured_sources = lambda cfg: ["qobuz"]
        v.selected_source = "qobuz"
        v._setup_prompt = ft.Container()

        v.refresh_setup_state(update=False)
        # Should not launch _refresh_landing_page since landing is already populated and fresh
        v.page.run_task.assert_not_called()

        # When marked stale, it triggers re-query
        v.mark_landing_stale()
        self.assertTrue(v._landing_stale)
        v.refresh_setup_state(update=False)
        self.assertEqual(v.page.run_task.call_count, 1)

        # Fresh but aged past the TTL (play counts move): re-queried too
        v.page.run_task.reset_mock()
        v._landing_stale = False
        v._landing_built_at = time.monotonic() - v._LANDING_TTL_S - 1
        v.refresh_setup_state(update=False)
        self.assertEqual(v.page.run_task.call_count, 1)


class TestDailySyncAndBatching(unittest.TestCase):
    def setUp(self):
        from utils.db_manager import DatabaseManager
        self.db = DatabaseManager(":memory:")
        asyncio.run(self.db.initialize())

    def tearDown(self):
        asyncio.run(self.db.close())

    def test_daily_release_sync_ledger_and_gates(self):
        import time
        import datetime

        # Initial check: no sync has ever run
        self.assertTrue(asyncio.run(self.db.should_run_daily_release_sync()))

        # Start a sync
        log_id = asyncio.run(self.db.log_release_sync_start(trigger_type="daily_auto"))
        latest = asyncio.run(self.db.get_latest_release_sync())
        self.assertEqual(latest["status"], "running")
        self.assertEqual(latest["trigger_type"], "daily_auto")

        # Running recently (< 1h) -> gate returns False to prevent duplicate runs
        self.assertFalse(asyncio.run(self.db.should_run_daily_release_sync()))

        # Finish sync today
        asyncio.run(self.db.log_release_sync_finish(log_id, artists_checked=10, new_found=2, status="completed"))
        latest = asyncio.run(self.db.get_latest_release_sync())
        self.assertEqual(latest["status"], "completed")
        self.assertEqual(latest["artists_checked"], 10)
        self.assertEqual(latest["new_found"], 2)

        # Finished today -> should NOT run again today
        self.assertFalse(asyncio.run(self.db.should_run_daily_release_sync()))

        # Simulate midnight crossing: finished yesterday, but only 2 hours ago (< 12h cooldown)
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        two_hours_ago = time.time() - (2 * 3600)
        async def _set_yesterday_recent():
            conn = await self.db.get_connection()
            await conn.execute("UPDATE release_sync_log SET sync_date = ?, finished_at = ? WHERE id = ?",
                               (yesterday, two_hours_ago, log_id))
            await conn.commit()
        asyncio.run(_set_yesterday_recent())
        # Should be protected by 12h cooldown
        self.assertFalse(asyncio.run(self.db.should_run_daily_release_sync()))

        # Simulate normal next day: finished yesterday, 14 hours ago (>= 12h cooldown)
        fourteen_hours_ago = time.time() - (14 * 3600)
        async def _set_yesterday_ready():
            conn = await self.db.get_connection()
            await conn.execute("UPDATE release_sync_log SET sync_date = ?, finished_at = ? WHERE id = ?",
                               (yesterday, fourteen_hours_ago, log_id))
            await conn.commit()
        asyncio.run(_set_yesterday_ready())
        self.assertTrue(asyncio.run(self.db.should_run_daily_release_sync()))

        # Simulate clock skew: finished_at is warped into the future
        future_time = time.time() + 7200
        async def _set_future_skew():
            conn = await self.db.get_connection()
            await conn.execute("UPDATE release_sync_log SET sync_date = ?, finished_at = ? WHERE id = ?",
                               (datetime.date.today().isoformat(), future_time, log_id))
            await conn.commit()
        asyncio.run(_set_future_skew())
        # Skew detected -> resets gate to True
        self.assertTrue(asyncio.run(self.db.should_run_daily_release_sync()))

    def test_batched_owned_titles_query(self):
        # Insert test artist, album, track
        async def _populate():
            conn = await self.db.get_connection()
            await conn.execute("INSERT INTO artists (id, name, track_count) VALUES (1, 'Burial', 2)")
            await conn.execute("INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'Untrue') ")
            await conn.execute("INSERT INTO tracks (id, album_id, title, path) VALUES (1, 1, 'Archangel', '/music/archangel.flac')")
            await conn.commit()
        asyncio.run(_populate())

        owned_map = asyncio.run(self.db.get_all_owned_artist_titles())
        self.assertIn("burial", owned_map)
        titles = owned_map["burial"]
        self.assertIn("Untrue", titles)
        self.assertIn("Archangel", titles)

    def test_library_counts(self):
        async def _populate():
            conn = await self.db.get_connection()
            await conn.execute("INSERT INTO artists (id, name, track_count) VALUES (1, 'Burial', 2)")
            await conn.execute("INSERT INTO albums (id, artist_id, title) VALUES (1, 1, 'Untrue')")
            await conn.execute("INSERT INTO tracks (id, album_id, title, path) VALUES (1, 1, 'Archangel', '/a.flac')")
            await conn.execute("INSERT INTO tracks (id, album_id, title, path) VALUES (2, 1, 'Near Dark', '/b.flac')")
            await conn.commit()
        asyncio.run(_populate())
        self.assertEqual(asyncio.run(self.db.get_library_counts()),
                         {"tracks": 2, "albums": 1, "artists": 1, "playlists": 0})

    def test_manual_refresh_button_ui_and_trigger(self):
        from ui.views.search import SearchView
        v = SearchView.__new__(SearchView)
        v.app = MagicMock()
        v.app.new_releases_refreshing = False
        v.app.db_manager = self.db
        v.mark_landing_stale = MagicMock()
        v.refresh_setup_state = MagicMock()

        # Idle state shows refresh icon button
        btn = asyncio.run(v._build_releases_refresh_button())
        self.assertIsInstance(btn, ft.IconButton)
        self.assertEqual(btn.icon, ft.Icons.REFRESH_ROUNDED)

        # Trigger manual refresh
        v._on_manual_releases_refresh()
        v.app.trigger_manual_new_releases_refresh.assert_called_once()
        v.mark_landing_stale.assert_called_once()
        v.refresh_setup_state.assert_called_with(force=True)

        # When refreshing, returns ProgressRing spinner
        v.app.new_releases_refreshing = True
        btn_refreshing = asyncio.run(v._build_releases_refresh_button())
        self.assertIsInstance(btn_refreshing, ft.Container)
        self.assertIsInstance(btn_refreshing.content, ft.ProgressRing)


class TestReleaseRefreshTrigger(unittest.TestCase):
    """main.py's pass scheduling: one pass at a time, manual re-checks every
    artist, and the landing re-check waits for boot and then runs hourly."""

    def setUp(self):
        import main
        self.main = main
        self.saved = dict(main._RELEASES_STATE)
        main._RELEASES_STATE.update(pending=False, running=False, gate_checked_at=None)
        self.addCleanup(main._RELEASES_STATE.update, self.saved)
        self.app = main.StreamripFletApp.__new__(main.StreamripFletApp)
        self.app.db_manager = MagicMock()
        self.app.search_view = MagicMock()

        async def _quiet():
            return None
        self.app._await_scroll_quiet = _quiet
        cfg = patch("utils.streamrip_api.load_config", lambda: {})
        cfg.start()
        self.addCleanup(cfg.stop)

    def _run(self, fn):
        async def _go():
            fn()
            # let the created task finish
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(_go())

    def test_manual_claims_running_synchronously_and_rechecks_everything(self):
        seen = {}

        async def _refresh(db, **kw):
            seen.update(kw)
            return 7

        with patch("utils.new_releases.refresh_new_releases", _refresh):
            async def _go():
                self.app.trigger_manual_new_releases_refresh()
                # the spinner reads this before the task has even started
                self.assertTrue(self.app.new_releases_refreshing)
                self.app.trigger_manual_new_releases_refresh()  # double tap: ignored
                for _ in range(5):
                    await asyncio.sleep(0)
            asyncio.run(_go())

        self.assertEqual(seen, {"trigger_type": "manual", "refresh_after_s": 0.0})
        self.assertFalse(self.app.new_releases_refreshing)
        self.assertFalse(self.main._RELEASES_STATE["pending"])
        self.app.search_view.on_new_releases_refreshed.assert_called_once_with(7)

    def test_gate_skip_clears_pending_and_does_not_notify(self):
        async def _no():
            return False
        self.app.db_manager.should_run_daily_release_sync = _no
        self._run(lambda: self.app.ensure_new_releases_refresh())
        self.assertFalse(self.main._RELEASES_STATE["pending"])
        self.app.search_view.on_new_releases_refreshed.assert_not_called()

    def test_landing_recheck_waits_for_boot_then_runs_hourly(self):
        self.app.ensure_new_releases_refresh = MagicMock()
        self.app.maybe_recheck_new_releases()  # boot hasn't checked yet
        self.app.ensure_new_releases_refresh.assert_not_called()

        self.main._RELEASES_STATE["gate_checked_at"] = time.monotonic()
        self.app.maybe_recheck_new_releases()  # checked just now
        self.app.ensure_new_releases_refresh.assert_not_called()

        self.main._RELEASES_STATE["gate_checked_at"] = time.monotonic() - self.main._RELEASES_RECHECK_S - 1
        self.app.maybe_recheck_new_releases()
        self.app.ensure_new_releases_refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
