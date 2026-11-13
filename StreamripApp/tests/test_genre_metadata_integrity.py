"""Regression tests for the genre-metadata integrity fixes.

Each test pins a failure that was found empirically on the real library, so the
docstrings name the concrete case rather than describing the rule abstractly.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.artist_graph import FAMILY_SIM, ROOT_SIM, tag_similarity
from utils.genre_similarity import build_npmi_model
from utils.genre_taxonomy import (
    genre_families, genre_bucket, genre_tokens, is_regional_tag,
)
from utils.metadata_enrich import split_artist_credits, credit_keys, _name_close


def _sim(a, b, npmi=None):
    """Stations' tag-to-tag similarity of two tokens."""
    return float(tag_similarity([a, b], npmi)[0, 1])


def _same_act(a, b):
    return bool(credit_keys(a) & credit_keys(b))


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


class TestTagSimilarity(unittest.TestCase):
    """Evidence gating alone over-corrected: with 224 subgenre tokens across 182
    artists, genuinely-related pairs are single-support too, so 'electrohouse'
    got fenced from 'deephouse' at 0.000. Compound heads and the curated
    taxonomy give them a floor the learned model can only raise."""

    def test_compound_genres_share_their_head(self):
        self.assertAlmostEqual(_sim("deephouse", "house"), ROOT_SIM, places=5)
        self.assertAlmostEqual(_sim("poprap", "rap"), ROOT_SIM, places=5)
        self.assertAlmostEqual(_sim("southernhiphop", "hiphop"), ROOT_SIM, places=5)

    def test_only_the_head_counts(self):
        """English genre names put the head last: 'alternative hip hop' is
        hip-hop, not 'alternative'; and 'garage' does not end in a genre called
        'rage' (the qualifier must be ≥ 3 letters)."""
        self.assertLess(_sim("alternativehiphop", "alternative"), ROOT_SIM)
        self.assertLess(_sim("garage", "rage"), ROOT_SIM)

    def test_same_family_is_never_unrelated(self):
        self.assertGreaterEqual(_sim("deephouse", "electrohouse"), FAMILY_SIM)
        self.assertGreaterEqual(_sim("eurohouse", "italohouse"), FAMILY_SIM)

    def test_learned_model_raises_never_lowers(self):
        self.assertAlmostEqual(_sim("trap", "drill", {"drill|trap": 0.7}), 0.7, places=5)
        self.assertAlmostEqual(
            _sim("deephouse", "electrohouse", {"deephouse|electrohouse": 0.01}), FAMILY_SIM,
            places=5)

    def test_cross_family_is_unrelated(self):
        """The Carti -> laiko timbre bridge."""
        self.assertEqual(_sim("trap", "laiko"), 0.0)
        self.assertEqual(_sim("hiphop", "rebetiko"), 0.0)

    def test_no_family_kinship_across_the_regional_line(self):
        """Folk/Cntry files laiko next to blues; that made a laiko station run
        into Fleetwood Mac once the Greek scene was used up."""
        self.assertEqual(genre_bucket("laiko"), genre_bucket("blues"))
        self.assertEqual(_sim("laiko", "blues"), 0.0)
        self.assertAlmostEqual(_sim("laiko", "rebetiko"), FAMILY_SIM, places=5)

    def test_families_use_primary_label_not_substring_matches(self):
        """'alternative hip hop' arrives separator-stripped, so the multi-label
        view matched 'alternative' -> Alt and linked Kendrick Lamar to
        Slipknot. The priority-ordered primary label resists that."""
        self.assertIn("Alt", genre_tokens("alternativehiphop"))   # the trap
        self.assertEqual(genre_bucket("alternativehiphop"), "Hip-Hop")
        kendrick = {"hiphop", "conscioushiphop", "jazzrap", "westcoasthiphop"}
        slipknot = {"numetal", "alternativemetal", "metal", "heavymetal"}
        self.assertFalse(genre_families(kendrick) & genre_families(slipknot))
        S = tag_similarity(sorted(kendrick | slipknot))
        tags = sorted(kendrick | slipknot)
        for a in kendrick:
            for b in slipknot:
                self.assertEqual(S[tags.index(a), tags.index(b)], 0.0, (a, b))


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
    """A scene is regional when it travels with a language, not when it is
    folk-adjacent: the Folk/Cntry bucket holds laiko AND blues."""

    def test_western_roots_music_is_not_regional(self):
        for tag in ("blues", "country", "americana", "bluegrass", "folkrock"):
            self.assertFalse(is_regional_tag(tag), f"{tag} is borderless")

    def test_language_bound_scenes_are_regional(self):
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

    def test_regional_vocabulary_covers_non_greek_scenes(self):
        """Cesária Evora is as regional as an artist gets, but Cape Verde's own
        genres were missing from the key list."""
        self.assertTrue(is_regional_tag("morna"))
        self.assertTrue(is_regional_tag("coladeira"))

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
        self.assertEqual(genre_bucket("Rock"), "Rock")
        self.assertEqual(genre_bucket("Hard Rock"), "Metal")
        self.assertTrue(genre_tokens("Rock") & genre_tokens("Hard Rock"))


if __name__ == "__main__":
    unittest.main()
