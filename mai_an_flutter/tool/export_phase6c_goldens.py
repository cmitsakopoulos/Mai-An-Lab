#!/usr/bin/env python3
"""Export Phase 6c reference goldens from StreamripApp Python modules.

Generates `test/goldens/phase6c.json` pinning:
- `quality_selector.hires_subtitle` across Qobuz, Deezer, mixed & default records
- `tagger.Container.FLAC.get_tag_pairs` field formatting & zero-padding
- `search.SearchView._pick_hero_result` scoring & tie-breaking cases
"""
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_APP = ROOT.parent / "StreamripApp"
sys.path.insert(0, str(PY_APP))

# Minimal flet stub so ui/player/quality_selector.py imports headlessly.
if "flet" not in sys.modules:
    ft = types.ModuleType("flet")
    ft.BottomSheet = object
    ft.Icons = types.SimpleNamespace(
        MUSIC_NOTE="music_note",
        ALBUM="album",
        HIGH_QUALITY="high_quality",
    )
    sys.modules["flet"] = ft

# Minimal ui.widgets / ui.tokens stubs if needed.
if "ui.widgets" not in sys.modules:
    w = types.ModuleType("ui.widgets")
    w.dialog_handoff = lambda *a, **kw: (None, None)
    sys.modules["ui.widgets"] = w

from ui.player.quality_selector import hires_subtitle  # noqa: E402
from utils.metadata_objects import AlbumMetadata, TrackMetadata  # noqa: E402
from utils.tagger import Container  # noqa: E402


def _pick_hero_py(query: str, tracks: list, albums: list, artists: list):
    """Direct extract of SearchView._pick_hero_result (ui/views/search.py:1906)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    candidates = []

    def get_name(item):
        return (item.get("ui_title") or item.get("name") or item.get("title") or "").strip().lower()

    for art in artists:
        name = (art.get("name") or "").strip().lower()
        if name and name == q:
            candidates.append((100.5, art))
    for trk in tracks:
        name = get_name(trk)
        if name and name == q:
            candidates.append((100.2, trk))
    for alb in albums:
        name = get_name(alb)
        if name and name == q:
            candidates.append((100.1, alb))

    for trk in tracks:
        name = get_name(trk)
        if not name or name == q:
            continue
        if name.startswith(q):
            candidates.append((75.0 + max(0.0, 15.0 * (len(q) / len(name))), trk))
        elif f" {q} " in f" {name} ":
            candidates.append((60.0, trk))
        elif q in name:
            candidates.append((45.0, trk))

    for alb in albums:
        name = get_name(alb)
        if not name or name == q:
            continue
        if name.startswith(q):
            candidates.append((74.0 + max(0.0, 15.0 * (len(q) / len(name))), alb))
        elif f" {q} " in f" {name} ":
            candidates.append((59.0, alb))
        elif q in name:
            candidates.append((44.0, alb))

    for art in artists:
        name = (art.get("name") or "").strip().lower()
        if not name or name == q:
            continue
        if name.startswith(q):
            candidates.append((74.5 + max(0.0, 15.0 * (len(q) / len(name))), art))
        elif f" {q} " in f" {name} ":
            candidates.append((58.0, art))
        elif q in name:
            candidates.append((43.0, art))

    if candidates:
        candidates.sort(key=lambda c: c[0], reverse=True)
        return candidates[0][1]

    if tracks:
        return tracks[0]
    if albums:
        return albums[0]
    if artists:
        return artists[0]
    return None


def main() -> None:
    hires_cases = [
        {"name": "qobuz_only", "records": [{"source": "qobuz"}]},
        {"name": "deezer_only", "records": [{"source": "deezer"}]},
        {"name": "mixed", "records": [{"source": "qobuz"}, {"source": "deezer"}]},
        {"name": "missing_source_defaults_qobuz", "records": [{}]},
        {"name": "deezer_uppercase", "records": [{"source": "DEEZER"}]},
    ]
    for c in hires_cases:
        c["expected"] = hires_subtitle(c["records"])

    album = AlbumMetadata(
        id="alb_1",
        album="Untrue",
        artist="Burial",
        year="2007",
        tracktotal=13,
        disctotal=1,
        genres=["Electronic", "UK Garage"],
        copyright="(P) 2007 Hyperdub",
    )
    track = TrackMetadata(
        id="trk_2",
        title="Archangel",
        artist="Burial",
        album=album,
        tracknumber=2,
        discnumber=1,
        isrc="GBHYP0700002",
        composer="William Bevan",
        lyrics="Holding you",
    )
    flac_pairs = [[k, v] for k, v in Container.FLAC.get_tag_pairs(track)]

    hero_cases = [
        {
            "name": "exact_artist_beats_exact_track_and_album",
            "query": "Burial",
            "tracks": [{"id": "t1", "media_type": "track", "ui_title": "Burial"}],
            "albums": [{"id": "a1", "media_type": "album", "ui_title": "Burial"}],
            "artists": [{"id": "ar1", "media_type": "artist", "name": "Burial"}],
        },
        {
            "name": "exact_track_beats_exact_album",
            "query": "Untrue",
            "tracks": [{"id": "t1", "media_type": "track", "ui_title": "Untrue"}],
            "albums": [{"id": "a1", "media_type": "album", "ui_title": "Untrue"}],
            "artists": [],
        },
        {
            "name": "shorter_prefix_wins_by_ratio",
            "query": "arch",
            "tracks": [
                {"id": "t1", "media_type": "track", "ui_title": "Archangel (Extended Mix)"},
                {"id": "t2", "media_type": "track", "ui_title": "Archangel"},
            ],
            "albums": [],
            "artists": [],
        },
        {
            "name": "word_boundary_beats_substring",
            "query": "dark",
            "tracks": [
                {"id": "t1", "media_type": "track", "ui_title": "Darkness Falls"},
                {"id": "t2", "media_type": "track", "ui_title": "Near Dark"},
            ],
            "albums": [],
            "artists": [],
        },
        {
            "name": "fallback_order_track_then_album_then_artist",
            "query": "zzzz",
            "tracks": [],
            "albums": [{"id": "a1", "media_type": "album", "ui_title": "Geogaddi"}],
            "artists": [{"id": "ar1", "media_type": "artist", "name": "Boards of Canada"}],
        },
    ]
    for c in hero_cases:
        picked = _pick_hero_py(c["query"], c["tracks"], c["albums"], c["artists"])
        c["expected_id"] = picked["id"] if picked else None

    out = {
        "hires_subtitle": hires_cases,
        "flac_tag_pairs": flac_pairs,
        "hero_result": hero_cases,
    }
    dest = ROOT / "test" / "goldens" / "phase6c.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
