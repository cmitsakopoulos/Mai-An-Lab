"""Unit tests for the station / walk (`track_graph.walk` over
`artist_graph.Drift`).

These use an in-memory fake db_manager with hand-placed coordinates and cover
the contract:

  • seed exclusion, length bound, avoid-set, determinism, empty for an
    unanalysed seed;
  • GENRE ranks artists, sound only breaks ties and picks the segue — a
    near-in-sound track from an unrelated genre never plays;
  • the segue is tempo (octave-folded) and energy, with timbre as the fallback
    for a track with no tempo;
  • the drift: a station leaves the seed's genre only through an artist that
    overlaps both, and only once it has been playing that way;
  • collaborations place untagged artists; culture discounts but doesn't wall;
  • per-artist / per-album repeat caps, collab credits counted against each act;
  • graceful degradation with no enrichment at all.

`test_walk_real_library.py` runs the same walk over a real library image.
"""

import asyncio
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock utils.config so tests don't touch the user's real app dir.
import utils.config as _cfg
_cfg.APP_DIR = "/tmp/dsptest_app_dir"
os.makedirs(_cfg.APP_DIR, exist_ok=True)

from utils import track_graph as tg


class FakeDB:
    """Minimal stand-in for DatabaseManager: per-track coordinates + display
    rows (what `load_live_coordinate_graph` reads) and per-track artist
    enrichment (what the artist graph reads)."""

    def __init__(self):
        self.rows: list[dict] = []
        self.meta: dict[str, dict] = {}
        self.genre_model: dict = {}

    def add(self, path, vec, artist, genres=(), country=None, album=None, title=None,
            bpm=None, energy=None):
        """A track at direction `vec` (2-D, normalised by the loader)."""
        self.rows.append({
            "path": path, "pca_coords": list(map(float, vec)), "artist": artist,
            "album": album or path, "title": title or path, "bpm": bpm, "energy": energy,
        })
        self.meta[path] = {"artist": artist, "country": country, "genres": frozenset(genres)}

    async def get_tracks_pca_coords(self):
        return [dict(r) for r in self.rows]

    async def get_artist_meta_for_paths(self, paths):
        return {p: self.meta[p] for p in paths if p in self.meta}

    async def get_genre_affinity(self):
        return self.genre_model


class CoordsOnlyDB(FakeDB):
    """A backend without the enrichment accessors at all."""

    def __getattribute__(self, name):
        if name in ("get_artist_meta_for_paths", "get_genre_affinity"):
            raise AttributeError(name)
        return super().__getattribute__(name)


def _run(coro):
    tg.invalidate_coord_graph_cache()
    return asyncio.run(coro)


def _at(deg):
    r = np.deg2rad(deg)
    return (np.cos(r), np.sin(r))


def _house_db():
    """SEED is house. Two house acts sit far from it in sound; a laiko act sits
    right next to it."""
    db = FakeDB()
    db.add("SEED", _at(0), "Seed Act", {"house"})
    db.add("H1", _at(60), "House One", {"deephouse", "house"})
    db.add("H2", _at(80), "House Two", {"house"})
    db.add("L1", _at(2), "Laiko Act", {"laiko"})
    db.add("L2", _at(4), "Laiko Act", {"laiko"}, album="L-other")
    return db


class TestWalkContract(unittest.TestCase):
    def test_seed_excluded_and_length_bounded(self):
        out = _run(tg.walk(_house_db(), "SEED", length=2))
        self.assertNotIn("SEED", out)
        self.assertEqual(len(out), 2)

    def test_no_duplicates_and_deterministic(self):
        db = _house_db()
        a = _run(tg.walk(db, "SEED", length=10))
        self.assertEqual(len(a), len(set(a)))
        self.assertEqual(a, _run(tg.walk(db, "SEED", length=10)))

    def test_avoid_set_respected(self):
        out = _run(tg.walk(_house_db(), "SEED", length=5, avoid={"H1"}))
        self.assertNotIn("H1", out)

    def test_unanalysed_seed_gives_nothing(self):
        self.assertEqual(_run(tg.walk(_house_db(), "NOT-ANALYSED", length=5)), [])


class TestGenreRanksSoundBreaksTies(unittest.TestCase):
    def test_unrelated_genre_never_plays_however_close_in_sound(self):
        """The Carti→laiko bridge: sound alone links them; genre does not."""
        out = _run(tg.walk(_house_db(), "SEED", length=10))
        self.assertEqual(set(out), {"H1", "H2"})

    def test_sound_breaks_a_genre_tie(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"})
        db.add("FAR", _at(90), "Far Act", {"house"})
        db.add("NEAR", _at(20), "Near Act", {"house"})
        self.assertEqual(_run(tg.walk(db, "SEED", length=2)), ["NEAR", "FAR"])

    def test_an_artist_plays_its_track_closest_to_the_previous_one(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"})
        db.add("A-far", _at(120), "Other Act", {"house"}, album="a1")
        db.add("A-near", _at(10), "Other Act", {"house"}, album="a2")
        self.assertEqual(_run(tg.walk(db, "SEED", length=1)), ["A-near"])


class TestSegue(unittest.TestCase):
    def _db(self, near_bpm, far_bpm):
        """One artist with two tracks: one near the seed in timbre, one far."""
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"}, bpm=124, energy=0.8)
        db.add("NEAR", _at(10), "Other Act", {"house"}, album="a1", bpm=near_bpm, energy=0.8)
        db.add("FAR", _at(120), "Other Act", {"house"}, album="a2", bpm=far_bpm, energy=0.8)
        return db

    def test_tempo_beats_timbre(self):
        self.assertEqual(_run(tg.walk(self._db(90, 125), "SEED", length=1)), ["FAR"])

    def test_half_and_double_time_share_a_pulse(self):
        self.assertEqual(_run(tg.walk(self._db(100, 62), "SEED", length=1)), ["FAR"])

    def test_energy_counts_too(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"}, bpm=124, energy=0.9)
        db.add("CALM", _at(10), "Other Act", {"house"}, album="a1", bpm=124, energy=0.5)
        db.add("LOUD", _at(120), "Other Act", {"house"}, album="a2", bpm=124, energy=0.88)
        db.add("PAD", _at(200), "Pad Act", {"rock"}, bpm=124, energy=0.7)
        self.assertEqual(_run(tg.walk(db, "SEED", length=1)), ["LOUD"])

    def test_no_tempo_falls_back_to_timbre(self):
        self.assertEqual(_run(tg.walk(self._db(None, 125), "SEED", length=1)), ["NEAR"])


class TestDrift(unittest.TestCase):
    def _bridge_db(self):
        """House acts, one act that is house AND synthpop (the bridge), a
        synthpop act with no house in it, and a laiko act."""
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"})
        for i in range(6):
            db.add(f"H{i}", _at(10 + i), f"House {i}", {"house"})
        db.add("X1", _at(30), "Bridge Act", {"house", "synthpop"}, album="x1")
        db.add("X2", _at(31), "Bridge Act", {"house", "synthpop"}, album="x2")
        db.add("Y1", _at(40), "Synth Act", {"synthpop"}, album="y1")
        db.add("Y2", _at(41), "Synth Act", {"synthpop"}, album="y2")
        db.add("Z1", _at(1), "Laiko Act", {"laiko"})
        return db

    def test_leaves_the_genre_only_through_an_overlapping_artist(self):
        out = _run(tg.walk(self._bridge_db(), "SEED", length=20, max_per_artist=0))
        self.assertIn("Y1", out, "the drift never moved on once house ran out")
        first_y = min(out.index(p) for p in ("Y1", "Y2") if p in out)
        first_x = min(out.index(p) for p in ("X1", "X2") if p in out)
        houses = [out.index(f"H{i}") for i in range(6)]
        self.assertLess(first_x, first_y, "reached synthpop without the bridge")
        self.assertLess(max(houses), first_y, "left house while house was left")
        self.assertNotIn("Z1", out)

    def test_focused_stays_under_the_seed_umbrella_and_ends(self):
        out = _run(tg.walk(self._bridge_db(), "SEED", length=20, max_per_artist=0,
                           focused=True))
        self.assertTrue({f"H{i}" for i in range(6)} <= set(out))
        self.assertTrue({"X1", "X2"} <= set(out), "the bridge act is still house")
        self.assertFalse({"Y1", "Y2", "Z1"} & set(out), "left the umbrella")
        self.assertEqual(len(out), 8)


class TestHubTags(unittest.TestCase):
    def test_a_tag_everyone_carries_links_less_than_a_rare_one(self):
        """Two acts sharing only 'pop' (most of the library has it) are less
        alike than two sharing a rare tag."""
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"pop", "eurodance"})
        db.add("RARE", _at(50), "Rare Act", {"eurodance", "softrock"})
        db.add("HUB", _at(10), "Hub Act", {"pop", "softrock"})
        for i in range(6):
            db.add(f"P{i}", _at(90 + i), f"Pop {i}", {"pop"})
        self.assertEqual(_run(tg.walk(db, "SEED", length=1)), ["RARE"])


class TestArtistEvidence(unittest.TestCase):
    def test_collaborator_places_an_untagged_artist(self):
        """An untagged act credited with the seed's artist belongs near it,
        even when an unrelated act sounds closer."""
        db = FakeDB()
        db.add("SEED", _at(0), "Mad Act", {"trap"})
        db.add("FEAT", _at(5), "Mad Act, Quiet Act", set(), album="f")
        db.add("QUIET", _at(70), "Quiet Act", set())
        db.add("ROCK", _at(1), "Rock Act", {"rock"})
        out = _run(tg.walk(db, "SEED", length=2))
        self.assertEqual(set(out), {"FEAT", "QUIET"})

    def _namesake_db(self, manual=False):
        """'Moose' is a Greek trapper whose row matched a British shoegaze band
        (GB, shoegaze). Every act he is credited with is Greek."""
        db = FakeDB()
        db.add("SEED", _at(0), "Indie Act", {"shoegaze", "dreampop"}, country="GB")
        db.add("INDIE", _at(80), "Other Indie", {"shoegaze"}, country="GB")
        db.add("MOOSE", _at(5), "Moose", {"shoegaze", "dreampop"}, country="GB")
        db.meta["MOOSE"]["manual"] = manual
        db.add("F1", _at(6), "Moose, Kalo", set(), country="GR", album="f1")
        db.add("F2", _at(7), "Vlo, Moose", set(), country="GR", album="f2")
        db.add("KALO", _at(8), "Kalo", {"trap"}, country="GR")
        db.add("VLO", _at(9), "Vlo", {"trap"}, country="GR")
        return db

    def test_collaborators_overturn_a_namesake_row(self):
        """Collaborators who all share a culture outvote the row's country, and
        the row's genres go with it: Moose never enters an indie station."""
        out = _run(tg.walk(self._namesake_db(), "SEED", length=3, focused=True))
        self.assertEqual(out, ["INDIE"])

    def test_a_hand_set_row_is_never_overturned(self):
        out = _run(tg.walk(self._namesake_db(manual=True), "SEED", length=1))
        self.assertEqual(out, ["MOOSE"])

    def test_a_collab_credit_takes_its_members_genres_not_its_own_row(self):
        """'Toquel, Light' looked up as one string matched British prog rock.
        Its acts are in the library, so they decide what it is."""
        db = FakeDB()
        db.add("SEED", _at(0), "Prog Band", {"progressiverock"})
        db.add("PROG", _at(80), "Prog Two", {"progressiverock"})
        db.add("COLLAB", _at(2), "Toq, Lite", {"progressiverock"}, album="c")
        db.add("TOQ", _at(60), "Toq", {"trap"})
        db.add("LITE", _at(61), "Lite", {"trap"})
        out = _run(tg.walk(db, "SEED", length=3, focused=True))
        self.assertEqual(out, ["PROG"])

    def test_culture_discounts_but_does_not_wall(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"trap"}, country="GR")
        db.add("GR1", _at(50), "Local Act", {"trap"}, country="GR")
        db.add("US1", _at(10), "Abroad Act", {"trap"}, country="US")
        self.assertEqual(_run(tg.walk(db, "SEED", length=2)), ["GR1", "US1"])


class TestRepeatCaps(unittest.TestCase):
    def _one_act_db(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"})
        for i in range(4):
            db.add(f"A{i}", _at(5 + i), "Big Act", {"house"}, album=f"alb{i}")
        db.add("B", _at(60), "Other Act", {"house"})
        return db

    def test_artist_cap(self):
        out = _run(tg.walk(self._one_act_db(), "SEED", length=6, max_per_artist=2))
        self.assertEqual(sum(p.startswith("A") for p in out), 2)
        self.assertIn("B", out)

    def test_caps_disabled_with_zero(self):
        out = _run(tg.walk(self._one_act_db(), "SEED", length=6,
                           max_per_artist=0, max_per_album=0))
        self.assertEqual(sum(p.startswith("A") for p in out), 4)

    def test_album_cap(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"house"})
        db.add("T1", _at(5), "Big Act", {"house"}, album="one")
        db.add("T2", _at(6), "Big Act", {"house"}, album="one")
        db.add("T3", _at(7), "Big Act", {"house"}, album="two")
        out = _run(tg.walk(db, "SEED", length=3, max_per_album=1))
        self.assertEqual(sorted(out), ["T1", "T3"])

    def test_collab_credit_counts_against_the_same_act(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act", {"trap"})
        db.add("S1", _at(5), "21 Savage", {"trap"}, album="s1")
        db.add("S2", _at(6), "21 Savage & Metro Boomin", {"trap"}, album="s2")
        db.add("O", _at(60), "Other Act", {"trap"})
        out = _run(tg.walk(db, "SEED", length=3, max_per_artist=1))
        self.assertEqual(sum(p in ("S1", "S2") for p in out), 1)


class TestStationFeedback(unittest.TestCase):
    def test_rejecting_an_artist_steers_away_from_it(self):
        async def go():
            db = FakeDB()
            db.add("SEED", _at(0), "Seed Act", {"house"})
            db.add("A1", _at(5), "Act A", {"house"}, album="a1")
            db.add("A2", _at(6), "Act A", {"house"}, album="a2")
            db.add("B1", _at(40), "Act B", {"house"})
            st = await tg.open_station(db, "SEED", max_per_artist=0)
            first = await st.take(1)
            st.reject(first)
            return first, await st.take(1)
        tg.invalidate_coord_graph_cache()
        first, nxt = asyncio.run(go())
        self.assertEqual(first, ["A1"])
        self.assertEqual(nxt, ["B1"])


class TestGracefulDegradation(unittest.TestCase):
    def test_no_tags_anywhere_falls_back_to_sound(self):
        db = FakeDB()
        db.add("SEED", _at(0), "Seed Act")
        db.add("NEAR", _at(10), "Near Act")
        db.add("FAR", _at(100), "Far Act")
        self.assertEqual(_run(tg.walk(db, "SEED", length=2)), ["NEAR", "FAR"])

    def test_backend_without_enrichment_accessors(self):
        db = CoordsOnlyDB()
        db.add("SEED", _at(0), "Seed Act")
        db.add("NEAR", _at(10), "Near Act")
        self.assertEqual(_run(tg.walk(db, "SEED", length=1)), ["NEAR"])


if __name__ == "__main__":
    unittest.main()
