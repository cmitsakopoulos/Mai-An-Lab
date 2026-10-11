"""Bounded, provider-rolling retries for failed artist lookups.

A failed lookup used to write nothing, so the artist stayed "new" and was
fetched again on every boot forever. Now each failure is counted, successive
attempts roll between MusicBrainz and Qobuz, and the third failure parks the
artist at status='error' until a manual Sync."""

import importlib
import os
import tempfile

import pytest

from utils.db_manager import DatabaseManager


def _me():
    # Resolved per test, never at import: other suites swap and reload modules
    # in sys.modules, and a stale reference would dodge the monkeypatches.
    return importlib.import_module("utils.metadata_enrich")


async def enrich_pending(db, **kw):
    return await _me().enrich_pending(db, **kw)


async def enrich_library(db, **kw):
    return await _me().enrich_library(db, **kw)


def _mb_result(status="ok", genres=None, country="GR"):
    return {"status": status, "mbid": "m" if status != "error" else None,
            "country": country if status != "error" else None, "area": None,
            "genres": genres if genres is not None else [{"name": "greek rap", "count": 3}],
            "score": 100, "reason": "error: offline" if status == "error" else None}


@pytest.fixture
def providers(monkeypatch):
    """Scriptable fakes. `state['mb']` / `state['qobuz']` map artist → response
    ('error' | 'blank' | 'ok'); every call is logged in `state['calls']`."""
    state = {"mb": {}, "qobuz": {}, "calls": [], "qobuz_open_fails": False}

    class FakeMB:
        def __init__(self, session, contact=None):
            pass

        async def lookup_artist(self, name, with_genres=True):
            state["calls"].append(("musicbrainz", name))
            mode = state["mb"].get(name, "ok")
            if mode == "error":
                return _mb_result("error")
            if mode == "blank":
                return _mb_result("notfound", genres=[], country=None)
            return _mb_result()

    async def fake_open(*, raise_on_error=False):
        if state["qobuz_open_fails"]:
            raise RuntimeError("Qobuz client initialization failed (AuthError: no creds)")
        return object()

    async def fake_close(_c):
        pass

    async def fake_lookup(_c, name):
        state["calls"].append(("qobuz", name))
        mode = state["qobuz"].get(name, "ok")
        if mode == "error":
            return {"genres": [], "reason": "error: timeout"}
        if mode == "blank":
            return {"genres": [], "reason": "no_match"}
        return {"genres": [{"name": "house", "count": 4}], "reason": None}

    monkeypatch.setattr(_me(), "MusicBrainzClient", FakeMB)
    monkeypatch.setattr("utils.metadata_qobuz.open_client", fake_open)
    monkeypatch.setattr("utils.metadata_qobuz.close_client", fake_close)
    monkeypatch.setattr("utils.metadata_qobuz.lookup_artist_genres", fake_lookup)
    return state


async def _db_with(tmpdir, *names):
    db = DatabaseManager(os.path.join(tmpdir, "test.db"))
    await db.initialize()
    conn = await db.get_connection()
    for i, n in enumerate(names, 1):
        await conn.execute(
            "INSERT INTO artists (id, name, track_count) VALUES (?, ?, ?)", (i, n, 10 - i))
    await conn.commit()
    return db


async def _attempts(db, name):
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT status, attempts, source FROM artist_enrichment WHERE artist_name = ?",
        (name,),
    ) as cur:
        return await cur.fetchone()


@pytest.mark.asyncio
async def test_failures_roll_between_providers_then_park(providers):
    providers["mb"]["X"] = "error"
    providers["qobuz"]["X"] = "error"
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, "X")

        await enrich_pending(db, preferred="musicbrainz")
        assert tuple(await _attempts(db, "X")) == ("retry", 1, "musicbrainz")
        await enrich_pending(db, preferred="musicbrainz")
        assert tuple(await _attempts(db, "X")) == ("retry", 2, "qobuz")
        summary = await enrich_pending(db, preferred="musicbrainz")
        assert tuple(await _attempts(db, "X")) == ("error", _me().ENRICH_MAX_ATTEMPTS, "musicbrainz")
        assert summary["parked"] == 1
        assert providers["calls"] == [
            ("musicbrainz", "X"), ("qobuz", "X"), ("musicbrainz", "X")]

        # Parked: the automatic pass leaves it alone, a manual Sync still finds it.
        assert (await enrich_pending(db))["status"] == "uptodate"
        assert len(providers["calls"]) == 3
        assert "X" in await db.get_artists_needing_enrichment(include_failed=True)
        await db.close()


@pytest.mark.asyncio
async def test_offline_abort_costs_no_attempt(providers):
    names = ["A", "B", "C", "D"]
    for n in names:
        providers["mb"][n] = "error"
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, *names)
        summary = await enrich_pending(db, preferred="musicbrainz")
        assert summary["status"] == "aborted"
        # The run of errors that tripped the abort is the connection, not the
        # artists: nothing is charged, and Qobuz isn't hammered after it.
        for n in names:
            assert await _attempts(db, n) is None
        assert all(p == "musicbrainz" for p, _ in providers["calls"])
        assert len(providers["calls"]) == 3
        await db.close()


@pytest.mark.asyncio
async def test_failure_never_clobbers_a_real_answer():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, "HasCountry", "Mine")
        await db.upsert_artist_enrichment("HasCountry", country="GR", genres=[], status="ok")
        await db.set_manual_artist_enrichment("Mine", country="US", genres=["rap"])

        assert await db.record_enrichment_failure("HasCountry", source="musicbrainz") is None
        assert await db.record_enrichment_failure("Mine", source="musicbrainz") is None
        assert (await db.get_artist_enrichment("HasCountry"))["country"] == "GR"
        assert (await db.get_artist_enrichment("Mine"))["source"] == "manual"
        await db.close()


@pytest.mark.asyncio
async def test_success_resets_attempts(providers):
    providers["mb"]["X"] = "error"
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, "X")
        await enrich_pending(db, preferred="musicbrainz")
        assert tuple(await _attempts(db, "X"))[:2] == ("retry", 1)
        await enrich_pending(db, preferred="musicbrainz")   # attempt 1 → Qobuz, ok
        status, attempts, source = await _attempts(db, "X")
        assert (status, attempts, source) == ("ok", 0, "qobuz")
        await db.close()


@pytest.mark.asyncio
async def test_unavailable_provider_hands_artists_over(providers):
    """Qobuz not logged in: its turn goes to MusicBrainz instead of being lost."""
    providers["mb"]["X"] = "error"
    providers["qobuz_open_fails"] = True
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, "X", "New")
        await enrich_pending(db, preferred="musicbrainz")   # X: retry 1
        providers["mb"]["X"] = "ok"
        providers["calls"].clear()
        summary = await enrich_pending(db, preferred="musicbrainz")
        assert ("musicbrainz", "X") in providers["calls"]
        assert (await db.get_artist_enrichment("X"))["status"] == "ok"
        assert summary["status"] == "completed"
        await db.close()


@pytest.mark.asyncio
async def test_blank_answer_from_fallback_counts_as_a_failure(providers):
    providers["mb"]["X"] = "error"
    providers["qobuz"]["X"] = "blank"
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, "X")
        await enrich_pending(db, preferred="musicbrainz")   # MB errors
        await enrich_pending(db, preferred="musicbrainz")   # Qobuz has nothing
        # Not a final 'lowconfidence' row: MusicBrainz gets its next turn.
        assert tuple(await _attempts(db, "X"))[:2] == ("retry", 2)
        assert not await db.get_low_confidence_artists()
        providers["mb"]["X"] = "ok"
        await enrich_pending(db, preferred="musicbrainz")
        assert (await db.get_artist_enrichment("X"))["country"] == "GR"
        await db.close()


@pytest.mark.asyncio
async def test_manual_sync_records_failures_too(providers):
    providers["mb"]["X"] = "error"
    with tempfile.TemporaryDirectory() as tmpdir:
        db = await _db_with(tmpdir, "X")
        summary = await enrich_library(db, provider="musicbrainz", include_failed=True)
        assert summary["status"] == "completed"
        assert tuple(await _attempts(db, "X"))[:2] == ("retry", 1)
        await db.close()
