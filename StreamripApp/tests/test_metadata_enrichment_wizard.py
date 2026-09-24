"""Unit tests for Metadata Enrichment Wizard DB methods, manual override protection, and MusicBrainz search candidate logic."""

import os
import json
import pytest
import tempfile
import asyncio

from utils.db_manager import DatabaseManager
from utils.metadata_enrich import _extract_genres, _extract_country, _closest_match, _looks_like_junk


@pytest.mark.asyncio
async def test_manual_artist_enrichment_and_protection():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = DatabaseManager(db_path)
        await db.initialize()

        # 1. Set manual artist enrichment
        await db.set_manual_artist_enrichment(
            "Kanye West", country="US", genres=["hip hop", "rap", "trap"]
        )

        res = await db.get_artist_enrichment("Kanye West")
        assert res is not None
        assert res["artist_name"] == "Kanye West"
        assert res["country"] == "US"
        assert res["source"] == "manual"
        assert res["status"] == "ok"
        genres = res["genres"]
        assert len(genres) == 3
        assert genres[0]["name"] == "hip hop"

        # 2. Automated MusicBrainz sync attempt should NOT overwrite manual row
        await db.upsert_artist_enrichment(
            "Kanye West", country="GB", genres=[{"name": "tribute", "count": 1}],
            source="musicbrainz", status="lowconfidence", force=False
        )

        res_after = await db.get_artist_enrichment("Kanye West")
        assert res_after["source"] == "manual"
        assert res_after["country"] == "US"
        assert res_after["genres"][0]["name"] == "hip hop"

        await db.close()


@pytest.mark.asyncio
async def test_gap_and_low_confidence_queries():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = DatabaseManager(db_path)
        await db.initialize()

        # Insert test artists in artists table
        conn = await db.get_connection()
        await conn.execute("INSERT INTO artists (id, name, track_count) VALUES (1, 'Artist A', 10)")
        await conn.execute("INSERT INTO artists (id, name, track_count) VALUES (2, 'Artist B', 5)")
        await conn.execute("INSERT INTO artists (id, name, track_count) VALUES (3, 'Artist C', 2)")
        await conn.commit()

        # Artist A has low confidence
        await db.upsert_artist_enrichment(
            "Artist A", country="US", genres=[{"name": "pop", "count": 1}],
            source="musicbrainz", score=60, status="lowconfidence"
        )

        # Artist B has empty genres (gap)
        await db.upsert_artist_enrichment(
            "Artist B", country="GR", genres=[],
            source="musicbrainz", score=90, status="ok"
        )

        # Artist C has manual override
        await db.set_manual_artist_enrichment("Artist C", country="GB", genres=["drill"])

        # Check gap artists (should include Artist B, but exclude Artist C which is manual)
        gaps = await db.get_metadata_gap_artists()
        gap_names = [g["artist_name"] for g in gaps]
        assert "Artist B" in gap_names
        assert "Artist C" not in gap_names

        # Check low confidence artists
        lows = await db.get_low_confidence_artists()
        low_names = [l["artist_name"] for l in lows]
        assert "Artist A" in low_names
        assert "Artist B" not in low_names

        # Confirm match for Artist A
        await db.confirm_artist_match("Artist A", country="US", genres=[{"name": "pop", "count": 1}], status="ok")
        lows_after = await db.get_low_confidence_artists()
        assert "Artist A" not in [l["artist_name"] for ll in lows_after for l in [ll]]

        await db.close()


def test_junk_and_closest_match_filtering():
    junk_artist = {"name": "Kanye West Tribute Band", "disambiguation": "tribute act"}
    assert _looks_like_junk(junk_artist) is True

    real_artist = {"name": "Ye", "aliases": [{"name": "Kanye West"}], "disambiguation": ""}
    assert _looks_like_junk(real_artist) is False

    match = _closest_match("Kanye West", [junk_artist, real_artist])
    assert match is not None
    assert match["name"] == "Ye"


@pytest.mark.asyncio
async def test_enrichment_progress_recording_and_propagation():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = DatabaseManager(db_path)
        await db.initialize()

        conn = await db.get_connection()
        aid = await db._get_or_create_artist(conn, "Artist SyncTest")
        await conn.execute(
            "INSERT INTO albums (artist_id, title, genre) VALUES (?, ?, ?)",
            (aid, "Test Album", "Pop"),
        )
        await conn.commit()

        # Before sync: artist needs enrichment
        needing_before = await db.get_artists_needing_enrichment(include_failed=True)
        assert "Artist SyncTest" in needing_before

        # Sync records progress to DB (status='ok' even if empty genres)
        await db.upsert_artist_enrichment(
            "Artist SyncTest", country="US", genres=[{"name": "trap", "count": 5}],
            source="musicbrainz", score=100, status="ok",
        )

        # After sync: artist is NO LONGER in needing list (progress recorded permanently)
        needing_after = await db.get_artists_needing_enrichment(include_failed=True)
        assert "Artist SyncTest" not in needing_after

        # Verify metadata propagation to album genre bucket
        summary = await db.fix_and_normalize_track_genres()
        assert summary["scanned"] >= 1

        async with conn.execute(
            "SELECT genre_bucket FROM albums WHERE artist_id = ?", (aid,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert "Hip-Hop" in row[0] or "Pop" in row[0]

        await db.close()



# ── Regressions from the 2026-09 metadata overhaul ───────────────────────────

def test_diacritics_do_not_split_a_name():
    """'Cesária Evora' must match MusicBrainz's 'Cesária Évora'.

    `str.isalnum()` is True for 'é', so the old alnum-only key kept accents and
    rejected a score-100 hit with a country on one combining mark, storing an
    empty 'lowconfidence' row instead."""
    from utils.metadata_enrich import _name_close, _norm_name
    assert _norm_name("Cesária Evora") == _norm_name("Cesária Évora")
    assert _name_close("Cesária Evora", "Cesária Évora")
    assert _name_close("Bjork", "Björk")
    assert not _name_close("Kanye West", "Taylor Swift")


def test_credit_string_never_falls_back_to_a_whole_string_guess():
    """A multi-artist credit must resolve to its members or to nothing.

    MusicBrainz fuzzy-matches the literal text of a credit like
    'Toquel, Fly Lo, Beyond' and returns whatever scores highest — live, that
    was the Hong Kong band Beyond, so Greek rap was stored as cantopop. Those
    rows feed the NPMI corpus and the walk's pool gate regardless of status, so
    a guess here re-fences real queues."""
    import asyncio
    from utils.metadata_enrich import MusicBrainzClient

    junk_hit = {
        "id": "x", "name": "Beyond", "score": 100,
        "country": "HK", "disambiguation": "Hong Kong rock band",
        "genres": [{"name": "cantopop", "count": 5}],
    }

    class FakeClient(MusicBrainzClient):
        def __init__(self):
            self.session = None
            self.user_agent = "test"
            self.min_interval = 0
            self._last = 0.0
            self._lock = asyncio.Lock()
            self._artist_memo = {}

        async def _get(self, path, params, retries=3):
            # The whole credit string fuzzy-matches a famous unrelated act
            # (this is what MusicBrainz really does); the individual members
            # resolve to nothing, as the Greek artists really do.
            q = (params or {}).get("query", "")
            if "Toquel, Fly Lo, Beyond" in q:
                return {"artists": [junk_hit]}, 200
            return {"artists": []}, 200

    res = asyncio.run(
        FakeClient()._lookup_artist_uncached("Toquel, Fly Lo, Beyond")
    )
    assert res["status"] == "notfound", res
    assert res["genres"] == []
    assert res["country"] is None
    assert res["reason"] == "credit_unresolved"

    # A SOLO name with no genuine match may still use the weak fallback: there
    # the top hit is at least a plausible single entity.
    class SoloClient(FakeClient):
        async def _get(self, path, params, retries=3):
            return {"artists": [junk_hit]}, 200

    solo = asyncio.run(SoloClient()._lookup_artist_uncached("Beyonce"))
    assert solo["status"] == "lowconfidence"
    assert solo["reason"] == "weak_match"


@pytest.mark.asyncio
async def test_gap_severity_reads_file_tags_and_country():
    """Severity must reflect whether a HUMAN is needed, not which columns are
    empty. An artist whose files say 'Hip-Hop', or who has a country, is not a
    blocking gap — the old grading flagged 70 artists where 11 needed a person."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(os.path.join(tmpdir, "t.db"))
        await db.initialize()
        conn = await db.get_connection()

        async def mk(name, genre=None):
            aid = await db._get_or_create_artist(conn, name)
            await conn.execute(
                "INSERT INTO albums (artist_id, title, genre) VALUES (?,?,?)",
                (aid, f"{name} LP", genre),
            )
            await conn.commit()

        await mk("Blocked", None)            # nothing anywhere
        await mk("HasFiles", "Hip-Hop")      # files know the answer
        await mk("HasCountry", None)
        await mk("Placeholder", "Divers")    # junk tag == no tag
        await db.upsert_artist_enrichment("HasCountry", country="GR", genres=[], status="ok")

        gaps = {g["artist_name"]: g for g in await db.get_metadata_gap_artists()}
        assert gaps["Blocked"]["gap_severity"] == db.GAP_BLOCKING
        assert gaps["Placeholder"]["gap_severity"] == db.GAP_BLOCKING
        assert gaps["HasFiles"]["gap_severity"] == db.GAP_SUGGESTED
        assert gaps["HasFiles"]["source_genres"] == ["Hip-Hop"]
        # A country is enough to place and fence an artist: not a nag.
        assert gaps["HasCountry"]["gap_severity"] == db.GAP_THIN

        cov = await db.get_metadata_coverage(gaps=list(gaps.values()))
        assert cov["blocking"] == 2
        assert cov["placeable"] == cov["artists"] - 2
        await db.close()


@pytest.mark.asyncio
async def test_bulk_tag_merges_rather_than_overwrites():
    """Dropping artists onto a genre bin must not erase a country already found
    for some of them, and must not blank genres when tagging a country."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(os.path.join(tmpdir, "t.db"))
        await db.initialize()
        await db.upsert_artist_enrichment("A", country="GR", genres=[], status="ok")
        await db.upsert_artist_enrichment(
            "B", country=None, genres=[{"name": "trap", "count": 2}], status="ok")

        n = await db.bulk_tag_artists(["A", "B"], genre="hip hop", refresh_model=False)
        assert n == 2
        a = await db.get_artist_enrichment("A")
        b = await db.get_artist_enrichment("B")
        assert a["country"] == "GR"                       # preserved
        assert [g["name"] for g in a["genres"]] == ["hip hop"]
        assert {g["name"] for g in b["genres"]} == {"trap", "hip hop"}   # merged

        await db.bulk_tag_artists(["B"], country="GR", refresh_model=False)
        b2 = await db.get_artist_enrichment("B")
        assert b2["country"] == "GR"
        assert {g["name"] for g in b2["genres"]} == {"trap", "hip hop"}  # kept
        await db.close()


@pytest.mark.asyncio
async def test_retry_incomplete_is_its_own_scope():
    """`include_failed` used to be DOCUMENTED as re-fetching rows that came back
    with empty genres while the SQL only matched status='error', so the
    documented healing never happened."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(os.path.join(tmpdir, "t.db"))
        await db.initialize()
        conn = await db.get_connection()
        for name in ("Empty", "Tagged", "Mine"):
            await db._get_or_create_artist(conn, name)
        await conn.commit()
        await db.upsert_artist_enrichment("Empty", country="GR", genres=[], status="ok")
        await db.upsert_artist_enrichment(
            "Tagged", country="US", genres=[{"name": "rock", "count": 1}], status="ok")
        await db.set_manual_artist_enrichment("Mine", country="GR", genres=[])

        assert "Empty" not in await db.get_artists_needing_enrichment(include_failed=True)
        retry = await db.get_artists_needing_enrichment(retry_incomplete=True)
        assert "Empty" in retry
        assert "Tagged" not in retry
        assert "Mine" not in retry          # hand entries are never re-fetched
        await db.close()


@pytest.mark.asyncio
async def test_metadata_edit_rebuilds_the_journey_graph():
    """THE regression this overhaul exists for.

    `build_journey_graph` was called only from a full acoustic rebuild and the
    bulk-override path, so a sync, a Save, an Accept and a Use-This-Match all
    left the journey graph — the artefact `walk()` actually traverses, and which
    is memory-cached — stale. Edits appeared to save and changed nothing."""
    import utils.metadata_enrich as me

    calls = []

    class FakeDB:
        async def fix_and_normalize_track_genres(self):
            calls.append("genres")
            return {}

    async def fake_affinity(db):
        calls.append("affinity")
        return 7

    async def fake_journey(db):
        calls.append("journey")
        return 11

    import utils.track_graph as tg
    orig_a, orig_j = tg.build_genre_affinity, tg.build_journey_graph
    tg.build_genre_affinity, tg.build_journey_graph = fake_affinity, fake_journey
    try:
        out = await me._do_refresh_walk_models(FakeDB())
    finally:
        tg.build_genre_affinity, tg.build_journey_graph = orig_a, orig_j

    assert calls == ["genres", "affinity", "journey"], calls
    assert out["journey_nodes"] == 11


def test_composite_bucket_labels_are_not_split_into_orphans():
    """'Rock/Alt' is one of this app's own coarse bucket labels, not two genres.

    These tokens are no longer just UI suggestions — the enrichment cascade
    writes them into artist_enrichment whenever MusicBrainz has nothing, and the
    walk reads that table. Splitting on the slash produced 'Alt', which nothing
    else in the corpus uses, so it would sit in the NPMI model as a singleton
    with no learned relation to the library's 'alternative rock'."""
    from utils.db_manager import _clean_source_tags as clean
    assert clean(["Rock/Alt"]) == ["rock"]
    assert clean(["Soul/R&B"]) == ["soul"]
    assert clean(["Folk/Cntry"]) == ["folk"]
    assert clean(["Alt"]) == ["alternative rock"]
    # A genuine multi-tag source list still splits.
    assert clean(["Rock, Metal, Pop"]) == ["Rock", "Metal", "Pop"]
    # Placeholders and localisations still behave.
    assert clean(["Hip-Hop", "Divers"]) == ["Hip-Hop"]
    assert clean(["\u00e9lectronique"]) == ["Electronic"]


def test_qobuz_geographic_slugs_never_become_genres():
    """Qobuz organises 'Musiques du monde' GEOGRAPHICALLY, so its subgenre slugs
    are mostly country names.

    A Greek pop singer with a German release came back tagged 'grece' and
    'allemagne'. Country is a separate field the walk reads differently
    (`_pool_foreign`'s regional fence), and a country name sitting in the genre
    set would pose as a musical style in the NPMI corpus."""
    from utils.metadata_qobuz import _tokens_for_genre as toks
    assert toks({"slug": "grece", "path": [94, 300], "id": 300}) == ["world"]
    assert toks({"slug": "allemagne", "path": [94, 301], "id": 301}) == ["world"]
    # Genuine styles under the same family survive, mapped to corpus tokens.
    assert toks({"slug": "afrique", "path": [94, 95], "id": 95}) == ["african"]
    assert toks({"slug": "amerique-latine", "path": [94, 96], "id": 96}) == ["latin"]


def test_qobuz_slugs_fold_onto_the_corpus_vocabulary():
    """Localised or abbreviated slugs must not enter the corpus verbatim — an
    unmapped spelling is a singleton in the NPMI model with no learned relation
    to anything (see get_genre_vocabulary)."""
    from utils.metadata_qobuz import _tokens_for_genre as toks
    assert toks({"slug": "rap-hip-hop", "path": [133], "id": 133}) == ["hip hop"]
    assert toks({"slug": "alternatif-et-inde", "path": [112, 119, 113], "id": 113}) == ["alternative rock"]
    assert toks({"slug": "electro", "path": [64, 71], "id": 71}) == ["electronic"]
    assert toks({"slug": "rb", "path": [127, 128], "id": 128}) == ["rhythm and blues"]
    assert toks({"slug": "bandes-originales-de-films", "path": [91, 92], "id": 92}) == ["soundtrack"]
    # Placeholder buckets carry no information at all.
    assert toks({"slug": "divers", "path": [1], "id": 1}) == []
    assert toks({"slug": "enfants", "path": [167], "id": 167}) == []
    # A leaf that is already an English genre passes straight through.
    assert toks({"slug": "house", "path": [64, 68], "id": 68}) == ["house"]
    # Unknown genre object shapes must never raise.
    assert toks({}) == []
    assert toks(None) == []
