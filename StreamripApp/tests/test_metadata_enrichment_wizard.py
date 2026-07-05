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
