"""Metadata audit (contradictions, duplicates) and library-only artist merges."""

import json
import os
import tempfile

import pytest

from utils.db_manager import DatabaseManager
from utils import metadata_audit as audit


async def _db(tmpdir, tracks):
    """tracks: [(artist, album, title, genre)]"""
    db = DatabaseManager(os.path.join(tmpdir, "t.db"))
    await db.initialize()
    for i, (artist, album, title, genre) in enumerate(tracks):
        await db.insert_track_hierarchical({
            "artist": artist, "album": album, "title": title, "genre": genre,
            "path": f"/m/{i}.flac", "duration": 200.0,
        })
    return db


async def _artist_tracks(db, name):
    conn = await db.get_connection()
    async with conn.execute(
        "SELECT t.path, t.artist_tag, al.title FROM tracks t JOIN albums al ON al.id = t.album_id "
        "JOIN artists ar ON ar.id = al.artist_id WHERE ar.name = ? ORDER BY t.path", (name,)) as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def _counts(db, name):
    conn = await db.get_connection()
    async with conn.execute("SELECT album_count, track_count FROM artists WHERE name = ?",
                            (name,)) as cur:
        r = await cur.fetchone()
    return tuple(r) if r else None


# ── skeleton ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("Χάρις Αλεξίου", "Haris Alexiou"),
    ("Λευτέρης Πανταζής", "Lefteris Pantazis"),
    ("Λάμπρος Καρελάς", "Lampros Karelas"),
    ("Alexiou Haris", "Haris Alexiou"),
    ("ΜΑΡΙΝΑ ΣΑΤΤΙ", "Marina Satti"),
    ("Χρήστος Μάστορας", "Christos Mastoras"),
])
def test_skeleton_meets_across_spellings(a, b):
    assert audit.skeleton(a) == audit.skeleton(b)


def test_skeleton_keeps_different_artists_apart():
    assert audit.skeleton("Mad Clip") != audit.skeleton("Snik")
    assert audit.skeleton("Νίκος Βέρτης") != audit.skeleton("Νίκος Οικονομόπουλος")


# ── duplicates ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicates_propose_the_bigger_name_and_respect_rejections():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [
            ("Haris Alexiou", "Odos Nefelis 88", "a", ""),
            ("Haris Alexiou", "Odos Nefelis 88", "b", ""),
            ("Χάρις Αλεξίου", "Η Αγάπη", "c", ""),
            ("Various Artists", "Hits", "d", ""),
            ("Various artists ", "Hits 2", "e", ""),
        ])
        dups = await audit.duplicate_artists(db)
        assert dups == [{"alias": "Χάρις Αλεξίου", "canonical": "Haris Alexiou",
                         "alias_tracks": 1, "canonical_tracks": 2}]
        await db.reject_alias_pair("Haris Alexiou", "Χάρις Αλεξίου")
        assert await audit.duplicate_artists(db) == []
        await db.close()


# ── merge / separate ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_merge_moves_albums_fills_enrichment_and_separates_exactly():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [
            ("Haris Alexiou", "Odos Nefelis 88", "a", ""),
            ("Χάρις Αλεξίου", "Odos Nefelis 88", "b", ""),   # same album title → one album
            ("Χάρις Αλεξίου", "Η Αγάπη", "c", ""),
        ])
        await db.upsert_artist_enrichment("Χάρις Αλεξίου", country="GR", genres=[{"name": "laiko", "count": 1}],
                                          source="musicbrainz", status="ok", force=True,
                                          provenance={"country": "musicbrainz", "genres": "musicbrainz"})
        await db.upsert_artist_enrichment("Haris Alexiou", genres=[{"name": "entechno", "count": 1}],
                                          source="musicbrainz", status="ok", force=True,
                                          provenance={"genres": "musicbrainz"})

        await db.merge_artists("Χάρις Αλεξίου", "Haris Alexiou")
        assert await _counts(db, "Χάρις Αλεξίου") is None
        assert await _counts(db, "Haris Alexiou") == (2, 3)
        tracks = await _artist_tracks(db, "Haris Alexiou")
        assert [t[1] for t in tracks] == [None, "Χάρις Αλεξίου", "Χάρις Αλεξίου"]
        enr = await db.get_artist_enrichment("Haris Alexiou")
        assert enr["country"] == "GR"                       # filled from the alias
        assert [g["name"] for g in enr["genres"]] == ["entechno"]   # kept its own
        assert await db.get_artist_enrichment("Χάρις Αλεξίου") is None
        assert await db.get_artist_aliases("Haris Alexiou") == ["Χάρις Αλεξίου"]
        # Library search finds every track under the kept name.
        assert len(await db.get_all_tracks(search_query="Haris")) == 3

        await db.separate_artist("Χάρις Αλεξίου")
        assert await _counts(db, "Haris Alexiou") == (1, 1)
        assert await _counts(db, "Χάρις Αλεξίου") == (2, 2)
        assert [t[1] for t in await _artist_tracks(db, "Χάρις Αλεξίου")] == [None, None]
        assert (await db.get_artist_enrichment("Χάρις Αλεξίου"))["country"] == "GR"
        assert await db.get_artist_aliases("Haris Alexiou") == []
        # Separating says "not the same": never proposed again.
        assert await audit.duplicate_artists(db) == []
        await db.close()


@pytest.mark.asyncio
async def test_scan_after_merge_files_tracks_under_the_kept_name():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Lefteris Pantazis", "A", "a", ""),
                           ("Λευτέρης Πανταζής", "B", "b", "")])
        await db.merge_artists("Λευτέρης Πανταζής", "Lefteris Pantazis")
        # A rescan of the old file and a newly added one, both tagged with the alias.
        await db.insert_tracks_batch([
            {"artist": "Λευτέρης Πανταζής", "album": "B", "title": "b", "path": "/m/1.flac"},
            {"artist": "ΛΕΥΤΈΡΗΣ ΠΑΝΤΑΖΉΣ", "album": "C", "title": "c", "path": "/m/9.flac"},
        ])
        assert await _counts(db, "Λευτέρης Πανταζής") is None
        tracks = await _artist_tracks(db, "Lefteris Pantazis")
        assert len(tracks) == 3
        assert {t[1] for t in tracks} == {None, "Λευτέρης Πανταζής", "ΛΕΥΤΈΡΗΣ ΠΑΝΤΑΖΉΣ"}
        # Both spellings go back on separate.
        await db.separate_artist("Λευτέρης Πανταζής")
        assert await _counts(db, "Λευτέρης Πανταζής") == (2, 2)
        await db.close()


@pytest.mark.asyncio
async def test_merge_chains_follow_the_kept_name():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("A", "x", "1", ""), ("B", "y", "2", ""), ("C", "z", "3", "")])
        await db.merge_artists("A", "B")
        await db.merge_artists("B", "C")
        assert sorted(await db.get_artist_aliases("C")) == ["A", "B"]
        await db.insert_track_hierarchical({"artist": "A", "album": "w", "title": "4",
                                            "path": "/m/new.flac"})
        assert await _counts(db, "C") == (4, 4)
        await db.close()


@pytest.mark.asyncio
async def test_merge_refuses_unknown_or_same_artist():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("A", "x", "1", "")])
        with pytest.raises(ValueError):
            await db.merge_artists("A", "A")
        with pytest.raises(ValueError):
            await db.merge_artists("A", "Nobody")
        assert await _counts(db, "A") == (1, 1)   # rolled back
        await db.close()


# ── contradictions ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_flags_script_tags_and_web_disagreements():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [
            ("Wrong Match", "Ανήκω", "Φωτιά", ""), ("Wrong Match", "Δρόμος", "Νύχτα", ""),
            ("Laiko Guy", "Songs", "One", "Laiko"),
            ("Web Says", "Album", "Song", ""),
            ("Mine", "Ανήκω", "Φωτιά", ""), ("Mine", "Δρόμος", "Νύχτα", ""),
            ("Fine", "Ανήκω", "Φωτιά", ""),
        ])
        for name, cc, src in [("Wrong Match", "US", "musicbrainz"), ("Laiko Guy", "FR", "musicbrainz"),
                              ("Web Says", "US", "musicbrainz"), ("Mine", "US", "manual"),
                              ("Fine", "GR", "musicbrainz")]:
            await db.upsert_artist_enrichment(name, country=cc, source=src, status="ok", force=True)
        await db.save_ai_research("Web Says", {"status": "ok", "country": "GB",
                                               "country_basis": "web"}, state="pending")
        flags = await audit.audit_flags(db)
        assert set(flags) == {"Wrong Match", "Laiko Guy", "Web Says"}   # manual never flagged
        assert flags["Wrong Match"][0]["rule"] == "script"
        assert flags["Wrong Match"][0]["suggest_country"] == "GR"
        assert flags["Laiko Guy"][0]["reason"] == "Tagged Laiko, but the country is FR."
        assert flags["Web Says"][0] == {"rule": "ai", "reason": "The web says GB; the library says US.",
                                        "suggest_country": "GB"}

        # "Country is right" hides it — until the country changes.
        await db.dismiss_audit_flag("Wrong Match", "script", "US")
        assert "Wrong Match" not in await audit.audit_flags(db)
        await db.upsert_artist_enrichment("Wrong Match", country="IT", source="musicbrainz",
                                          status="ok", force=True)
        assert "Wrong Match" in await audit.audit_flags(db)
        await db.close()


# ── workbench Review ────────────────────────────────────────────────────────

def _pane(db):
    import flet as ft
    from unittest.mock import MagicMock
    from ui.player.metadata_workbench import MetadataWorkbenchPane
    app = MagicMock()
    app.dismiss_dialog.return_value = True
    pane = MetadataWorkbenchPane.__new__(MetadataWorkbenchPane)
    pane.app, pane.db = app, db
    pane.vocab, pane.countries, pane.selected = [], [], set()
    pane.filter, pane.search, pane.sort = "all", "", "least"
    pane.all_gaps, pane.low_conf, pane.review_rows = [], [], []
    pane.everyone, pane.review_by_name = [], {}
    pane.coverage, pane.ai_pending, pane.researched, pane.ai_ready = {}, {}, set(), False
    pane._row_refs = {}
    pane._list = ft.Column()
    pane._safe_update = MagicMock()
    pane._merge = MagicMock()
    return pane, app


def _walk(ctrl):
    import flet as ft
    yield ctrl
    for attr in ("content", "controls"):
        sub = getattr(ctrl, attr, None)
        for c in (sub if isinstance(sub, list) else [sub]):
            if isinstance(c, ft.BaseControl):
                yield from _walk(c)


def _texts(ctrl):
    import flet as ft
    return [c.value for c in _walk(ctrl) if isinstance(c, ft.Text)]


def _button(ctrl, label):
    import flet as ft
    for c in _walk(ctrl):
        if isinstance(c, ft.Container) and getattr(c, "on_click", None) and label in _texts(c):
            return c
    raise AssertionError(f"no {label!r} button")


@pytest.mark.asyncio
async def test_review_rows_carry_every_reason_once_per_artist():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Haris Alexiou", "Odos Nefelis", "a", ""),
                           ("Haris Alexiou", "Odos Nefelis", "b", ""),
                           ("Χάρις Αλεξίου", "Η Αγάπη", "Φως", "")])
        await db.upsert_artist_enrichment("Χάρις Αλεξίου", country="US", source="musicbrainz",
                                          status="lowconfidence", force=True)
        pane, _app = _pane(db)
        pane.low_conf = await db.get_low_confidence_artists()
        rows = await pane._build_review_rows()
        assert [r["artist_name"] for r in rows] == ["Χάρις Αλεξίου"]
        assert [r["kind"] for r in rows[0]["review"]] == ["mb", "audit", "dup"]
        # The row shows the first reason and how many more.
        assert "MusicBrainz wasn't sure about this match.  +2" in _texts(pane._row(rows[0]))
        await db.close()


@pytest.mark.asyncio
async def test_editor_merges_under_the_chosen_name():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Haris Alexiou", "A", "a", ""), ("Haris Alexiou", "A", "b", ""),
                           ("Χάρις Αλεξίου", "B", "c", "")])
        pane, app = _pane(db)
        pane.review_rows = await pane._build_review_rows()
        pane._open_editor(pane.review_rows[0])
        sheet = app.page.show_dialog.call_args[0][0]
        assert "Same artist as Haris Alexiou?" in _texts(sheet)

        _button(sheet, "Merge").on_click(None)
        sheet.on_dismiss()   # the sheet closes, then the merge runs
        pane._merge.assert_called_once_with("Χάρις Αλεξίου", "Haris Alexiou")

        # Keeping the Greek spelling instead flips the direction.
        pane._merge.reset_mock()
        app.page.show_dialog.reset_mock()
        app.dismiss_dialog.reset_mock()
        pane._open_editor(pane.review_rows[0])
        sheet = app.page.show_dialog.call_args[0][0]
        pill = next(c for c in _walk(sheet) if getattr(c, "on_click", None)
                    and _texts(c) == ["Χάρις Αλεξίου"])
        pill.on_click(None)
        _button(sheet, "Merge").on_click(None)
        sheet.on_dismiss()
        pane._merge.assert_called_once_with("Haris Alexiou", "Χάρις Αλεξίου")
        await db.close()


# ── namesakes and hand-set genres ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_collaborators_flag_a_namesake_row():
    """'Moose' matched a British shoegaze band; the acts he's credited with
    here are Greek. Review offers GR plus his collaborators' genres."""
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [
            ("Moose", "Solo", "a", ""),
            ("Moose, Kalo", "F1", "b", ""),
            ("Vlo, Moose", "F2", "c", ""),
            ("Indie Band", "Album", "d", ""),
        ])
        await db.upsert_artist_enrichment("Moose", country="GB",
                                          genres=[{"name": "shoegaze", "count": 1}],
                                          source="musicbrainz", status="ok", score=100)
        for n in ("Moose, Kalo", "Vlo, Moose"):
            await db.upsert_artist_enrichment(n, country="GR",
                                              genres=[{"name": "trap", "count": 1}],
                                              source="musicbrainz", status="ok", score=100)
        await db.upsert_artist_enrichment("Indie Band", country="GB",
                                          genres=[{"name": "shoegaze", "count": 1}],
                                          source="musicbrainz", status="ok", score=100)
        flags = await audit.audit_flags(db)
        assert [f["rule"] for f in flags.get("Moose", [])] == ["collab"]
        f = flags["Moose"][0]
        assert f["suggest_country"] == "GR" and f["suggest_genres"] == ["trap"]
        assert "Indie Band" not in flags
        await db.close()


@pytest.mark.asyncio
async def test_set_genres_replaces_and_the_library_follows():
    """A correction replaces: the files' 'Metal' and the source's tags are gone
    from both auto-play's row and the Library's album genre/families."""
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Kasabian", "Velociraptor!", "a", "Metal"),
                           ("Other", "X", "b", "Metal")])
        await db.upsert_artist_enrichment("Kasabian", country="GB",
                                          genres=[{"name": "rock", "count": 3}],
                                          source="musicbrainz", status="ok", score=100)
        n = await db.set_artists_genres(["Kasabian"], ["Indie Rock", "dance-punk"])
        assert n == 1
        row = await db.get_artist_enrichment("Kasabian")
        assert [g["name"] for g in row["genres"]] == ["indie rock", "dance-punk"]
        assert row["country"] == "GB" and row["source"] == "manual"
        conn = await db.get_connection()
        async with conn.execute(
            "SELECT al.genre, al.genre_bucket FROM albums al JOIN artists ar "
            "ON ar.id = al.artist_id WHERE ar.name = 'Kasabian'") as cur:
            genre, bucket = await cur.fetchone()
        assert genre == "Indie Rock"
        assert "Metal" not in (bucket or "").split(",")
        async with conn.execute(
            "SELECT al.genre FROM albums al JOIN artists ar ON ar.id = al.artist_id "
            "WHERE ar.name = 'Other'") as cur:
            assert (await cur.fetchone())[0] == "Metal"   # nobody else touched
        await db.close()


@pytest.mark.asyncio
async def test_everything_lists_complete_and_hand_set_artists():
    with tempfile.TemporaryDirectory() as d:
        db = await _db(d, [("Done", "A", "a", ""), ("Gap", "B", "b", "")])
        await db.set_manual_artist_enrichment("Done", country="GB", genres=["rock"])
        gaps = {g["artist_name"] for g in await db.get_metadata_gap_artists()}
        every = {g["artist_name"]: g for g in
                 await db.get_metadata_gap_artists(everything=True)}
        assert gaps == {"Gap"}
        assert set(every) == {"Done", "Gap"}
        assert every["Done"]["gap_severity"] == db.GAP_NONE
        await db.close()


# ── All, sorted by how sure we are (replaced the Review tab) ────────────────

async def _namesake_pane(d):
    from unittest.mock import MagicMock
    db = await _db(d, [("Moose", "Solo", "a", ""), ("Moose, Kalo", "F1", "b", ""),
                       ("Vlo, Moose", "F2", "c", ""), ("Kasabian", "K", "d", ""),
                       ("Unsure", "U", "e", "")])
    await db.upsert_artist_enrichment("Moose", country="GB",
                                      genres=[{"name": "shoegaze", "count": 1}],
                                      source="musicbrainz", status="ok", score=100)
    for n in ("Moose, Kalo", "Vlo, Moose"):
        await db.upsert_artist_enrichment(n, country="GR", genres=[{"name": "trap", "count": 1}],
                                          source="musicbrainz", status="ok", score=100)
    await db.upsert_artist_enrichment("Unsure", country="SE", genres=[{"name": "pop", "count": 1}],
                                      source="musicbrainz", status="lowconfidence", score=70)
    await db.set_artists_genres(["Kasabian"], ["indie rock"], country="GB", refresh_model=False)
    pane, app = _pane(db)
    tasks = []
    app.page.run_task = tasks.append
    pane._render = MagicMock()
    pane._schedule_walk_refresh = MagicMock()
    pane._h_bins = MagicMock()
    pane._pending_focus = None

    async def drain():
        while tasks:
            await tasks.pop(0)()
    await pane._reload_async()
    return db, pane, drain


@pytest.mark.asyncio
async def test_all_sorts_doubtful_first_and_flips():
    with tempfile.TemporaryDirectory() as d:
        db, pane, _drain = await _namesake_pane(d)
        names = [g["artist_name"] for g in pane._visible()]
        assert names[:2] == ["Moose", "Unsure"]      # audit flag, then MB unsure
        assert names[-1] == "Kasabian"               # set by hand
        pane._toggle_sort()
        assert [g["artist_name"] for g in pane._visible()][0] == "Kasabian"
        # The reason shows on the row in All, not in a separate tab.
        moose = next(g for g in pane.everyone if g["artist_name"] == "Moose")
        assert any("Greek" in t for t in _texts(pane._row(moose)) if t)
        await db.close()


@pytest.mark.asyncio
async def test_selecting_doubtful_rows_offers_fix_and_looks_right():
    with tempfile.TemporaryDirectory() as d:
        db, pane, drain = await _namesake_pane(d)
        pane.selected = {"Moose", "Unsure", "Kasabian"}
        pane._render_bins()
        tray = pane._h_bins.content
        labels = _texts(tray)
        assert "Apply fix · 1" in labels and "Looks right · 2" in labels
        assert "Edit genres…" in labels

        _button(tray, "Apply fix · 1").on_click(None)   # only Moose has a fix
        await drain()
        e = await db.get_artist_enrichment("Moose")
        assert e["country"] == "GR"
        assert [g["name"] if isinstance(g, dict) else g for g in e["genres"]] == ["trap"]

        pane.selected = {"Unsure"}
        pane._render_bins()
        tray = pane._h_bins.content
        assert not any(t and t.startswith("Apply fix") for t in _texts(tray))
        _button(tray, "Looks right · 1").on_click(None)
        await drain()
        assert (await db.get_artist_enrichment("Unsure"))["status"] == "ok"
        assert not pane.review_by_name               # nothing doubtful left
        await db.close()


@pytest.mark.asyncio
async def test_bar_hides_until_selection_and_picker_replaces_or_adds():
    with tempfile.TemporaryDirectory() as d:
        db, pane, drain = await _namesake_pane(d)
        try:
            await _picker_flow(db, pane, drain)
        finally:
            await db.close()


async def _picker_flow(db, pane, drain):
    if True:
        prefs = {}
        pane.app._prefs, pane.app._save_pref = prefs, prefs.__setitem__
        pane.vocab = await db.get_genre_vocabulary(limit=300)
        pane.selected = set()
        pane._render_bins()
        assert pane._h_bins.visible is False

        pane.selected = {"Kasabian"}
        pane._render_bins()
        _button(pane._h_bins.content, "Edit genres…").on_click(None)
        sheet = pane.app.page.show_dialog.call_args[0][0]
        assert "indie rock  ✕" in _texts(sheet)          # what it has, removable
        field = next(c for c in _walk(sheet) if isinstance(c, __import__("flet").TextField))

        # Search ranks the exact/prefix match first; Enter picks it.
        field.value = "tra"
        field.on_change(type("E", (), {"control": field})())
        rows = [c.data for c in _walk(sheet) if isinstance(getattr(c, "data", None), str)]
        assert rows[0] == "trap" and rows[-1] == "tra"   # Create “tra” is last
        field.on_submit(type("E", (), {"control": field})())
        _button(sheet, "Replace genres for Kasabian").on_click(None)
        sheet.on_dismiss(None)
        await drain()
        e = await db.get_artist_enrichment("Kasabian")
        assert sorted(g["name"] if isinstance(g, dict) else g for g in e["genres"]) == \
            ["indie rock", "trap"]

        # Add mode leaves what they had alone and only shows the new picks.
        pane.selected = {"Unsure"}
        pane._render_bins()
        pane.app.page.show_dialog.reset_mock()
        _button(pane._h_bins.content, "Edit genres…").on_click(None)
        sheet = pane.app.page.show_dialog.call_args[0][0]
        seg = next(c for c in _walk(sheet) if type(c).__name__ == "CupertinoSegmentedBar")
        seg.on_change("add")
        next(c for c in _walk(sheet) if getattr(c, "data", None) == "trap").on_click(None)
        _button(sheet, "Add to Unsure").on_click(None)
        sheet.on_dismiss(None)
        await drain()
        e = await db.get_artist_enrichment("Unsure")
        assert sorted(g["name"] if isinstance(g, dict) else g for g in e["genres"]) == \
            ["pop", "trap"]
        assert pane._recent_genres()[0] == "trap"
