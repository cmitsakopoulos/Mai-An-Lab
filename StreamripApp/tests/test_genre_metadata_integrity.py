"""Regression tests for the genre-metadata integrity fixes.

Each test pins a failure that was found empirically on the real library, so the
docstrings name the concrete case rather than describing the rule abstractly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.genre_similarity import build_npmi_model, soft_set_sim, FAMILY_FLOOR
from utils.genre_taxonomy import (
    genre_families, genre_bucket, genre_tokens, is_regional_tag,
)
from utils.metadata_enrich import split_artist_credits, credit_keys, _name_close
from utils.track_graph import _same_act


class TestNpmiEvidenceGating(unittest.TestCase):
    """Raw NPMI returns exactly 1.0 — 'these are the same genre' — for two tags
    seen together on a single artist. On the real library that produced 90
    saturated pairs, 8 of them bridging different coarse families
    ('countryrock' ≡ 'swamprock', 'afrohouse' ≡ 'worldbeat')."""

    def test_single_artist_pair_is_not_certainty(self):
        # One artist carries the rare pair; the rest give the corpus a size, so
        # pab = 1/N < 1 and the NPMI denominator is non-degenerate.
        corpus = [{"countryrock", "swamprock"}] + [{"hiphop"}, {"metal"}, {"jazz"}]
        raw = build_npmi_model(corpus, min_support=1, shrinkage_k=0.0)
        self.assertEqual(
            raw.get("countryrock|swamprock"), 1.0,
            "raw NPMI calls a single-artist coincidence a perfect identity")

        gated = build_npmi_model(corpus)
        self.assertNotIn("countryrock|swamprock", gated)

    def test_repeated_evidence_survives_but_is_shrunk(self):
        corpus = [{"hiphop", "trap"} for _ in range(10)] + [{"jazz"}, {"metal"}]
        m = build_npmi_model(corpus)
        v = m.get("hiphop|trap")
        self.assertIsNotNone(v, "a 10-artist pair must survive gating")
        self.assertLess(v, 1.0, "support shrinkage must keep it below certainty")
        self.assertGreater(v, 0.5, "strong repeated evidence should stay strong")

    def test_no_saturated_pairs_under_default_gating(self):
        corpus = [{"a", "b"}, {"a", "b"}, {"c", "d"}, {"c", "d"}, {"e"}]
        m = build_npmi_model(corpus)
        self.assertTrue(all(v < 0.999 for v in m.values()))


class TestFamilyBackstop(unittest.TestCase):
    """Evidence gating alone over-corrected: with 224 subgenre tokens across 182
    artists, genuinely-related pairs are single-support too, so 'electrohouse'
    got fenced from 'deephouse' at 0.000. The curated taxonomy floors it."""

    def test_same_family_is_never_unrelated(self):
        self.assertGreaterEqual(
            soft_set_sim({"deephouse"}, {"electrohouse"}, {}), FAMILY_FLOOR)
        self.assertGreaterEqual(
            soft_set_sim({"eurohouse"}, {"italohouse"}, {}), FAMILY_FLOOR)

    def test_backstop_clears_the_walk_veto_floor(self):
        # track_graph.walk's default veto_genre_floor.
        self.assertGreater(FAMILY_FLOOR, 0.06)

    def test_cross_family_gets_no_floor(self):
        """The Carti -> laiko timbre bridge must still be fenced."""
        self.assertEqual(soft_set_sim({"trap"}, {"laiko"}, {}), 0.0)
        self.assertEqual(soft_set_sim({"hiphop"}, {"rebetiko"}, {}), 0.0)

    def test_backstop_is_opt_out(self):
        self.assertEqual(
            soft_set_sim({"deephouse"}, {"electrohouse"}, {}, family_floor=0.0), 0.0)

    def test_families_use_primary_label_not_substring_matches(self):
        """'alternative hip hop' arrives separator-stripped, so the multi-label
        view matched 'alternative' -> Rock/Alt and linked Kendrick Lamar to
        Slipknot. The priority-ordered primary label resists that."""
        self.assertIn("Rock/Alt", genre_tokens("alternativehiphop"))   # the trap
        self.assertEqual(genre_bucket("alternativehiphop"), "Hip-Hop")
        kendrick = {"hiphop", "conscioushiphop", "jazzrap", "westcoasthiphop"}
        slipknot = {"numetal", "alternativemetal", "metal", "heavymetal"}
        self.assertFalse(genre_families(kendrick) & genre_families(slipknot))
        self.assertEqual(soft_set_sim(kendrick, slipknot, {}), 0.0)


class TestCreditDecomposition(unittest.TestCase):
    """'Travis Scott/Metro Boomin/21 Savage' matched no MB entity, so the old
    code accepted the top non-junk hit — a GB drum-and-bass act — and those
    fabricated genres drove a hard veto against 21 Savage's own track."""

    def test_multi_artist_strings_decompose(self):
        self.assertEqual(
            split_artist_credits("Travis Scott/Metro Boomin/21 Savage"),
            ["Travis Scott", "Metro Boomin", "21 Savage"])
        self.assertEqual(
            split_artist_credits("21 Savage & Metro Boomin"),
            ["21 Savage", "Metro Boomin"])
        self.assertEqual(
            split_artist_credits("Dave, AJ Tracey"), ["Dave", "AJ Tracey"])

    def test_solo_artists_do_not_decompose(self):
        for name in ("Slipknot", "Playboi Carti", "Depeche Mode", "Sade"):
            self.assertEqual(split_artist_credits(name), [],
                             f"{name} must not be treated as a collab credit")

    def test_same_act_sees_through_credit_strings(self):
        self.assertTrue(_same_act("21 Savage", "21 Savage & Metro Boomin"))
        self.assertTrue(_same_act("21 Savage", "Travis Scott/Metro Boomin/21 Savage"))
        self.assertTrue(_same_act("AJ Tracey", "Dave, AJ Tracey"))
        self.assertFalse(_same_act("21 Savage", "Playboi Carti"))
        self.assertFalse(_same_act("Slipknot", "Kendrick Lamar"))

    def test_credit_keys_of_solo_artist_is_the_whole_name(self):
        self.assertEqual(credit_keys("Playboi Carti"), frozenset({"playboicarti"}))

    def test_name_close_returns_a_bool(self):
        """It fell off the end of the function and returned None."""
        self.assertIs(_name_close("Foo Bar", "Totally Different"), False)
        self.assertIs(_name_close("Kanye West", "Kanye West"), True)


class TestRegionalScenes(unittest.TestCase):
    """`_is_regional` gates a HARD cross-country pool constraint. It used to ask
    'is this the Folk/Cntry bucket?', but that bucket holds laiko AND blues, so
    Fleetwood Mac was treated as a regional act and fenced from every non-GB
    artist — 4.9% of all enriched pairs in the audit."""

    def test_western_roots_music_is_not_regional(self):
        from utils.track_graph import _is_regional
        self.assertFalse(_is_regional({"bluesrock", "folkrock", "poprock"}))
        for tag in ("blues", "country", "americana", "bluegrass", "folkrock"):
            self.assertFalse(is_regional_tag(tag), f"{tag} is borderless")

    def test_language_bound_scenes_are_regional(self):
        from utils.track_graph import _is_regional
        self.assertTrue(_is_regional({"folkpop", "laiko", "modernlaiko"}))
        self.assertTrue(_is_regional({"rebetiko"}))
        for tag in ("laiko", "rebetiko", "entechno", "reggaeton", "dancehall"):
            self.assertTrue(is_regional_tag(tag), f"{tag} travels with a language")

    def test_entechno_is_not_electronic(self):
        """'entechno' (Greek art-song) contains the substring 'techno', so the
        Electronic rule captured it — which also made it non-regional."""
        self.assertEqual(genre_bucket("entechno"), "Folk/Cntry")
        self.assertTrue(is_regional_tag("entechno"))

    def test_regional_keys_do_not_disturb_ordinary_buckets(self):
        self.assertEqual(genre_bucket("techno"), "Electronic")
        self.assertEqual(genre_bucket("electronica"), "Electronic")
        self.assertEqual(genre_bucket("Hard Rock"), "Metal")
        self.assertEqual(genre_bucket("Rap"), "Hip-Hop")

    def test_regional_requires_dominance_not_mere_presence(self):
        """One regional-flavoured tag on a borderless artist must not trip the
        hard country fence. Real profiles from the library, with their measured
        regional fractions."""
        from utils.track_graph import _is_regional
        borderless = {
            # AJ Tracey — a UK rapper; the lone 'dancehall' tag fenced him from
            # other UK rappers (0.20).
            "AJ Tracey": {"cloudrap", "dancehall", "grime", "hiphop", "ukdrill"},
            # Santana (0.12) and Pitbull (0.12) — international acts.
            "Santana": {"latinrock", "bluesrock", "classicrock", "jazzfusion",
                        "jazzrock", "poprock", "rock", "latin rock"},
            "Pitbull": {"latinpop", "dance", "dancepop", "electrohouse",
                        "electropop", "pop", "poprap", "rap"},
        }
        for name, tags in borderless.items():
            self.assertFalse(_is_regional(tags), f"{name} must not be regional")

        regional = {
            "Vasilis Karras": {"laiko", "rebetiko", "folkpop"},          # 0.67
            "Giorgos Mazonakis": {"laiko", "modernlaiko", "folkpop", "pop"},  # 0.50
            "Rina": {"jpop"},                                            # 1.00
        }
        for name, tags in regional.items():
            self.assertTrue(_is_regional(tags), f"{name} must be regional")

    def test_regional_vocabulary_covers_non_greek_scenes(self):
        """Cesária Evora is as regional as an artist gets, but Cape Verde's own
        genres were missing from the key list, so she scored 0.25 and read as
        borderless. Vocabulary coverage and the dominance threshold are separate
        fixes — this needs both."""
        from utils.track_graph import _is_regional
        evora = {"latin", "worldbeat", "morna", "coladeira",
                 "afrohouse", "folk", "modinha", "cabo"}
        self.assertTrue(is_regional_tag("morna"))
        self.assertTrue(is_regional_tag("coladeira"))
        self.assertTrue(_is_regional(evora))

    def test_added_regional_keys_have_no_substring_collisions(self):
        """Keys match as raw substrings against separator-stripped tags, so a
        short key can hide inside an unrelated genre ('rai' is excluded for
        exactly that reason)."""
        safe = ("morna", "coladeira", "fado", "enka", "bhangra")
        decoys = (
            "hiphop", "trap", "grime", "ukdrill", "dancepop", "electrohouse",
            "alternativerock", "heavymetal", "numetal", "drumandbass",
            "downtempo", "triphop", "shoegaze", "posthardcore", "synthpop",
            "deephouse", "eurodance", "classicrock", "bluesrock", "folkrock",
            "gangstarap", "southernhiphop", "conscioushiphop", "jazzfusion",
        )
        for d in decoys:
            for k in safe:
                self.assertNotIn(k, d, f"key {k!r} hides inside {d!r}")
            self.assertFalse(is_regional_tag(d), f"{d} wrongly reads as regional")


class TestOmissionVsAddition(unittest.TestCase):
    """The display rule: a source that DROPS a family is noise (Slipknot's
    'Iowa' tagged bare 'Rock'); a source that ADDS one is signal."""

    def test_multi_label_tokens_are_order_invariant(self):
        a = genre_tokens("Rock, Metal, Pop")
        for variant in ("Pop, Rock, Metal", "Metal, Pop, Rock", "Rock, Pop, Metal"):
            self.assertEqual(genre_tokens(variant), a)

    def test_single_label_collapse_is_the_bug_being_avoided(self):
        """Documents why the display must not use genre_bucket alone: two
        orderings of one semantic set are fine, but 'Rock' vs 'Hard Rock' —
        an ordinary source disagreement — flips the family outright."""
        self.assertEqual(genre_bucket("Rock"), "Rock/Alt")
        self.assertEqual(genre_bucket("Hard Rock"), "Metal")
        self.assertTrue(genre_tokens("Rock") & genre_tokens("Hard Rock"))


if __name__ == "__main__":
    unittest.main()
