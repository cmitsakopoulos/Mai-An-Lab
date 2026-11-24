#!/usr/bin/env python3
"""Golden values for phase 5 stage c1 (PORT_MAP rule 3): Flet's
`assistant_runner.AssistantRunner` deterministic half over scripted fakes
(random LCG, clock, library, engine, track_graph), plus `JARVIS_PHRASES`,
`difflib.SequenceMatcher.ratio`, `str.isdigit` and `str.capitalize`.

    test/goldens/assistant_runner.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_runner_goldens.py
"""
from __future__ import annotations

import asyncio
import copy
import datetime as real_datetime
import difflib
import json
import logging
import os
import random as real_random
import sys
import time as real_time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import assistant_intent as ai  # noqa: E402
from utils import assistant_runner as ar  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "assistant_runner.json")


class _ScriptedRandom(real_random.Random):
    """64-bit LCG (top 32 bits) matching test/assistant_runner_test.dart."""

    def __init__(self, seed: int):
        super().__init__()
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def _next(self) -> int:
        self._state = (
            self._state * 6364136223846793005 + 1442695040888963407
        ) & 0xFFFFFFFFFFFFFFFF
        return self._state >> 32

    def _randbelow(self, n: int) -> int:
        return self._next() % n

    def random(self) -> float:
        return self._next() / 4294967296.0


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, db: "_FakeLibrary"):
        self._db = db

    def execute(self, sql: str, params=()):
        # Only used by _apply_anaphora_resolution for artist NOCASE lookup.
        name = params[0] if params else ""
        target = name.lower()
        found = None
        for t in self._db._tracks:
            art = t.get("artist") or ""
            if art.lower() == target:
                found = {"path": t["path"], "title": t.get("title"), "artist": art}
                break
        return _FakeCursor(found)


class _FakeLibrary:
    def __init__(self, spec: dict, calls: list):
        self._tracks = [copy.deepcopy(t) for t in spec.get("tracks", [])]
        self._most_played = [copy.deepcopy(t) for t in spec.get("most_played", [])]
        self._analysed = set(spec.get("analysed", []))
        self._recent_played = set(spec.get("recent_played", []))
        self._walks = {k: list(v) for k, v in spec.get("walks", {}).items()}
        self._neighbors = {k: list(v) for k, v in spec.get("neighbors", {}).items()}
        self._playlists = dict(spec.get("playlists", {}))
        self._next_pid = max(self._playlists.values(), default=0) + 1
        self._calls = calls

    def _hydrate(self, t: dict, *, include_timbre: bool = False) -> dict:
        row = copy.deepcopy(t)
        has_timbre = row.pop("_timbre", False)
        if include_timbre and has_timbre:
            row["timbre"] = b"\x00\x01\x02\x03"
        return row

    async def search_tracks_simple(self, query: str, limit: int = 25) -> list[dict]:
        q = query.lower()
        out = []
        for t in self._tracks:
            hay = " ".join(
                str(t.get(k) or "") for k in ("title", "artist", "album")
            ).lower()
            if q in hay:
                out.append(self._hydrate(t))
                if len(out) >= limit:
                    break
        return out

    async def get_all_tracks(self) -> list[dict]:
        return [self._hydrate(t) for t in self._tracks]

    async def get_track_full(self, path: str):
        for t in self._tracks:
            if t.get("path") == path:
                return self._hydrate(t, include_timbre=True)
        return None

    async def has_current_features(self, path: str, version: int) -> bool:
        return path in self._analysed

    async def recent_played_paths(self, window_seconds: int = 0) -> set[str]:
        return set(self._recent_played)

    # Plain def: synchronous at call time so fire-and-forget order matches Dart.
    def record_playback(self, path: str, event: str, seed_path=None):
        self._calls.append(["record_playback", path, event, seed_path])
        self._recent_played.add(path)

    async def get_most_played(self, limit: int = 20) -> list[dict]:
        return [self._hydrate(t) for t in self._most_played[:limit]]

    async def get_all_artists(self, search_query: str = "") -> list[dict]:
        q = search_query.lower()
        seen = []
        for t in self._tracks:
            a = t.get("artist") or ""
            if a and q in a.lower() and a not in seen:
                seen.append(a)
        return [{"name": a} for a in seen]

    async def get_connection(self):
        return _FakeConn(self)

    async def create_playlist(self, name: str) -> int:
        if any(k.lower() == name.lower() for k in self._playlists):
            raise ValueError("UNIQUE constraint failed: playlists.name")
        pid = self._next_pid
        self._next_pid += 1
        self._playlists[name] = pid
        self._calls.append(["create_playlist", name, pid])
        return pid

    async def add_track_to_playlist(self, playlist_id: int, path: str) -> None:
        self._calls.append(["add_track_to_playlist", playlist_id, path])


class _FakeEngine:
    def __init__(self, spec: dict, calls: list):
        self.queue: list[dict] = [copy.deepcopy(t) for t in spec.get("queue", [])]
        self.current_index: int = spec.get("current_index", 0)
        self.current_path: str = spec.get("current_path", "")
        self.current_track: str = spec.get("current_track", "")
        self.current_artist: str = spec.get("current_artist", "")
        self.current_album: str = spec.get("current_album", "")
        self.duration: float = float(spec.get("duration", 0.0))
        self.is_playing: bool = bool(spec.get("is_playing", False))
        self._is_shuffle: bool = bool(spec.get("is_shuffle", False))
        self._calls = calls

    @property
    def is_shuffle(self) -> bool:
        return self._is_shuffle

    @is_shuffle.setter
    def is_shuffle(self, value: bool) -> None:
        self._is_shuffle = bool(value)
        self._calls.append(["is_shuffle", self._is_shuffle])

    def _sync_current(self) -> None:
        if 0 <= self.current_index < len(self.queue):
            t = self.queue[self.current_index]
            self.current_path = t.get("path") or ""
            self.current_track = t.get("track_title") or t.get("title") or ""
            self.current_artist = t.get("artist_name") or t.get("artist") or ""
            self.current_album = t.get("album_title") or t.get("album") or ""
            self.duration = float(t.get("duration") or 0.0)
        else:
            self.current_path = ""
            self.current_track = ""
            self.current_artist = ""
            self.current_album = ""
            self.duration = 0.0

    def set_queue(self, tracks: list[dict], start_index: int = 0) -> None:
        self._calls.append(["set_queue", [t.get("path") for t in tracks], start_index])
        self.queue = [copy.deepcopy(t) for t in tracks]
        self.current_index = (
            min(max(0, start_index), len(self.queue) - 1) if self.queue else 0
        )
        self._sync_current()

    def queue_last(self, track: dict) -> None:
        self._calls.append(["queue_last", track.get("path")])
        self.queue.append(copy.deepcopy(track))

    def queue_next(self, track: dict) -> None:
        self._calls.append(["queue_next", track.get("path")])
        at = min(self.current_index + 1, len(self.queue))
        self.queue.insert(at, copy.deepcopy(track))

    def queue_last_many(self, tracks: list[dict]) -> None:
        self._calls.append(["queue_last_many", [t.get("path") for t in tracks]])
        self.queue.extend(copy.deepcopy(t) for t in tracks)

    def remove_from_queue(self, index: int) -> None:
        self._calls.append(["remove_from_queue", index])
        if not (0 <= index < len(self.queue)):
            return
        removed_active = index == self.current_index
        self.queue.pop(index)
        if not self.queue:
            self.is_playing = False
            self.current_index = 0
            self._sync_current()
            return
        if index < self.current_index:
            self.current_index -= 1
        elif removed_active:
            self.current_index = min(self.current_index, len(self.queue) - 1)
            self._sync_current()

    def move_queue_item(self, old_index: int, new_index: int) -> None:
        self._calls.append(["move_queue_item", old_index, new_index])
        if not (
            0 <= old_index < len(self.queue) and 0 <= new_index < len(self.queue)
        ):
            return
        item = self.queue.pop(old_index)
        self.queue.insert(new_index, item)
        cur = self.current_index
        if old_index == cur:
            self.current_index = new_index
        elif old_index < cur <= new_index:
            self.current_index = cur - 1
        elif new_index <= cur < old_index:
            self.current_index = cur + 1

    def next(self) -> None:
        self._calls.append(["next"])

    def previous(self) -> None:
        self._calls.append(["previous"])

    def pause(self) -> None:
        self._calls.append(["pause"])
        self.is_playing = False

    def play(self) -> None:
        self._calls.append(["play"])
        self.is_playing = True

    def stop(self) -> None:
        self._calls.append(["stop"])
        self.is_playing = False
        self.current_path = ""
        self.current_track = ""
        self.current_artist = ""
        self.current_album = ""
        self.duration = 0.0

    def clear_queue(self) -> None:
        self._calls.append(["clear_queue"])
        self.is_playing = False
        self.queue.clear()
        self.current_index = 0
        self.current_path = ""
        self.current_track = ""
        self.current_artist = ""
        self.current_album = ""
        self.duration = 0.0


def _make_datetime_shim(clock: list[int]):
    fixed = real_datetime.datetime(*clock)

    class _FixedDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    mod = types.ModuleType("datetime")
    for k, v in vars(real_datetime).items():
        setattr(mod, k, v)
    mod.datetime = _FixedDateTime
    return mod, real_datetime.datetime(*clock, tzinfo=real_datetime.timezone.utc).timestamp()


def _fake_track_graph(lib: _FakeLibrary, calls: list):
    async def walk(db, seed, length=12, avoid=None):
        av = set(avoid or ())
        calls.append(["walk", seed, length, sorted(av)])
        return [p for p in lib._walks.get(seed, []) if p not in av][:length]

    async def neighbors(db, seed, k=10, edge_kind=None):
        calls.append(["neighbors", seed, k, edge_kind])
        return [{"path": p} for p in lib._neighbors.get(seed, [])[:k]]

    return types.SimpleNamespace(
        FEATURES_VERSION=ar.track_graph.FEATURES_VERSION,
        KIND_ARTIST=ar.track_graph.KIND_ARTIST,
        walk=walk,
        neighbors=neighbors,
    )


# ── Shared synthetic catalog ────────────────────────────────────────────────

CATALOG = [
    {"path": "/m/echoes_pf.flac", "title": "Echoes", "artist": "Pink Floyd",
     "album": "Meddle", "duration": 1412.0, "genre": "progressive rock",
     "bpm": 68.0, "energy": 0.35, "brightness": 0.3, "_timbre": True},
    {"path": "/m/echoes_ytg.flac", "title": "Echoes", "artist": "Young the Giant",
     "album": "Home of the Strange", "duration": 225.0, "genre": "indie rock",
     "bpm": 122.0, "energy": 0.72, "brightness": 0.65},
    {"path": "/m/time_pf.flac", "title": "Time", "artist": "Pink Floyd",
     "album": "The Dark Side of the Moon", "duration": 413.0, "genre": "rock",
     "bpm": 120.0, "energy": 0.55, "brightness": 0.5, "_timbre": True},
    {"path": "/m/money_pf.flac", "title": "Money", "artist": "Pink Floyd",
     "album": "The Dark Side of the Moon", "duration": 382.0, "genre": "rock",
     "bpm": 124.0, "energy": 0.65, "brightness": 0.6},
    {"path": "/m/paranoid.flac", "title": "Paranoid Android", "artist": "Radiohead",
     "album": "OK Computer", "duration": 387.0, "genre": "alternative rock",
     "bpm": 84.0, "energy": 0.6, "brightness": 0.45},
    {"path": "/m/karma.flac", "title": "Karma Police", "artist": "Radiohead",
     "album": "OK Computer", "duration": 264.0, "genre": "alternative rock",
     "bpm": 75.0, "energy": 0.4, "brightness": 0.4},
    {"path": "/m/airbag.flac", "title": "Airbag", "artist": "Radiohead",
     "album": "OK Computer", "duration": 284.0, "genre": "alternative rock",
     "bpm": 84.0, "energy": 0.7, "brightness": 0.55},
    {"path": "/m/ambient_rain.flac", "title": "Rainy Window", "artist": "LoFi Ghost",
     "album": "Chill Piano Study", "duration": 185.0, "genre": "ambient jazz",
     "bpm": 82.0, "energy": 0.2, "brightness": 0.25},
    {"path": "/m/heavy_metal.flac", "title": "Iron Fist", "artist": "Steel Wolf",
     "album": "Heavy Thrash", "duration": 240.0, "genre": "heavy metal",
     "bpm": 142.0, "energy": 0.96, "brightness": 0.62},
    {"path": "/m/single_no_album.mp3", "title": "Solo Sketch", "artist": "Αλέξης",
     "album": "", "duration": 45.0, "genre": "acoustic", "bpm": 0.0,
     "energy": None, "brightness": None},
    {"path": "/m/no_dur.mp3", "title": "Interlude", "artist": "Björk",
     "album": "Homogenic", "duration": 0.0, "genre": "electronic", "bpm": 95.0,
     "energy": 0.3, "brightness": 0.35},
]

BASE_LIB = {
    "tracks": CATALOG,
    "most_played": [CATALOG[0], CATALOG[4], CATALOG[9]],
    "analysed": ["/m/echoes_pf.flac", "/m/time_pf.flac", "/m/paranoid.flac"],
    "recent_played": ["/m/karma.flac"],
    "walks": {
        "/m/echoes_pf.flac": ["/m/time_pf.flac", "/m/paranoid.flac", "/m/karma.flac", "/m/ambient_rain.flac"],
        "/m/time_pf.flac": [],
        "/m/paranoid.flac": ["/m/airbag.flac", "/m/echoes_pf.flac"],
    },
    "neighbors": {
        "/m/echoes_pf.flac": [
            "/m/time_pf.flac", "/m/money_pf.flac", "/m/karma.flac",
            "/m/paranoid.flac", "/m/airbag.flac", "/m/ambient_rain.flac", "/m/heavy_metal.flac",
        ],
        "/m/single_no_album.mp3": [],
    },
    "playlists": {"Chill Vibe": 1},
}

Q3 = [
    {"path": "/m/echoes_pf.flac", "track_title": "Echoes", "artist_name": "Pink Floyd", "album_title": "Meddle", "duration": 1412.0},
    {"path": "/m/time_pf.flac", "track_title": "Time", "artist_name": "Pink Floyd", "album_title": "The Dark Side of the Moon", "duration": 413.0},
    {"path": "/m/money_pf.flac", "track_title": "Money", "artist_name": "Pink Floyd", "album_title": "The Dark Side of the Moon", "duration": 382.0},
]


def _far_history(entity_msg: dict, pad_pairs: int) -> list[dict]:
    msgs = [entity_msg]
    for i in range(pad_pairs):
        msgs.append({"sender": "user", "text": f"chatter {i}"})
        msgs.append({"sender": "assistant", "text": f"plain reply {i}"})
    return msgs


SCENARIOS = [
    {
        "name": "easter_eggs_and_transport",
        "seed": 101,
        "clock": [2026, 11, 24, 9, 5, 12],
        "library": BASE_LIB,
        "engine": {"queue": [], "is_playing": False, "is_shuffle": False},
        "turns": [
            {"text": "help"},
            {"text": "who made you"},
            {"text": "make me a coffee"},
            {"text": "open the pod bay doors"},
            {"text": "i am iron man"},
            {"text": "tell me a joke"},
            {"text": "what time is it"},
            {"text": "what time is it", "clock": [2026, 11, 24, 17, 45, 0]},
            {"text": "what time is it", "clock": [2026, 11, 24, 0, 7, 0]},
            {"text": "system status"},
            {"text": "thank you"},
            {"text": "give me a quote"},
            # Empty-queue transport
            {"text": "next song"},
            {"text": "previous"},
            {"text": "pause"},
            {"text": "resume"},
            # Populate queue via play_now then exercise transport
            {"text": "play Solo Sketch"},
            {"text": "resume"},
            {"text": "next song"},
            {"text": "previous"},
            {"text": "pause"},
            {"text": "pause"},
            {"text": "mute"},
            {"text": "unmute"},
            {"text": "shuffle"},
            {"text": "shuffle"},
            {"text": "stop playing"},
            {"text": "clear queue"},
        ],
    },
    {
        "name": "play_now_and_disambiguation",
        "seed": 202,
        "clock": [2026, 11, 24, 14, 0, 0],
        "library": BASE_LIB,
        "engine": {},
        "turns": [
            # Single match with album & duration
            {"text": "play Paranoid Android"},
            # Single match without album & short duration
            {"text": "play Solo Sketch"},
            # Single match with 0 duration
            {"text": "play Interlude"},
            # Multi match (Radiohead -> 3 tracks, random start)
            {"text": "play Radiohead"},
            # Disambiguation (Echoes -> Pink Floyd & Young the Giant) -> pick "2"
            {"text": "play Echoes"},
            {"text": "2"},
            # Disambiguation -> pick by artist/subtitle word
            {"text": "play Echoes"},
            {"text": "meddle"},
            # Disambiguation -> cancel with "no"
            {"text": "play Echoes"},
            {"text": "no"},
            # Disambiguation -> unrelated command cancels choice and runs
            {"text": "play Echoes"},
            {"text": "system status"},
            # Fuzzy match (> 0.65)
            {"text": "play Paranoid Androyd"},
            # Not found
            {"text": "play zzzzzqx"},
            # Unknown fallback: empty, exact, fuzzy, not found
            {"text": "   "},
            {"text": "Karma Police"},
            {"text": "Paranoid Androyd"},
            {"text": "completely unrecognizable gibberish"},
        ],
    },
    {
        "name": "queue_add_next_random_usual",
        "seed": 303,
        "clock": [2026, 11, 24, 19, 30, 0],
        "library": BASE_LIB,
        "engine": {},
        "turns": [
            # Empty queue -> queue_add starts playing
            {"text": "add Time to queue"},
            # Non-empty queue -> queue_add appends, queue_next inserts after current
            {"text": "add Money to queue"},
            {"text": "play Karma Police next"},
            {"text": "add nonexistent xyz to queue"},
            {"text": "surprise me"},
            {"text": "play the usual"},
        ],
    },
    {
        "name": "empty_library_branches",
        "seed": 304,
        "clock": [2026, 11, 24, 10, 0, 0],
        "library": {"tracks": [], "most_played": []},
        "engine": {},
        "turns": [
            {"text": "surprise me"},
            {"text": "play the usual"},
            {"text": "play something chill"},
            {"text": "hello jarvis"},
        ],
    },
    {
        "name": "greet_and_confirmations",
        "seed": 404,
        "clock": [2026, 11, 24, 8, 30, 0],
        "library": BASE_LIB,
        "engine": {},
        "turns": [
            # Several greetings to hit both < 0.5 (usual offer) and >= 0.5 (plain)
            {"text": "hello jarvis"},
            {"text": "yes"},
            {"text": "hi jarvis", "clock": [2026, 11, 24, 14, 0, 0]},
            {"text": "nah"},
            {"text": "hey jarvis", "clock": [2026, 11, 24, 21, 0, 0]},
            {"text": "yesa"},
            {"text": "hello", "clock": [2026, 11, 24, 21, 0, 0]},
            {"text": "go ahead and apply it"},
            {"text": "hello", "clock": [2026, 11, 24, 21, 0, 0]},
            {"text": "don't apply"},
            {"text": "hello", "clock": [2026, 11, 24, 21, 0, 0]},
            {"text": "system status"},
        ],
    },
    {
        "name": "queue_edit_and_save",
        "seed": 505,
        "clock": [2026, 11, 24, 12, 0, 0],
        "library": BASE_LIB,
        "engine": {
            "queue": Q3 + [
                {"path": "/m/paranoid.flac", "track_title": "Paranoid Android", "artist_name": "Radiohead", "duration": 387.0},
                {"path": "", "track_title": "Broken Row", "artist_name": "Nobody", "duration": 0.0},
            ],
            "current_index": 1,
            "current_path": "/m/time_pf.flac",
            "current_track": "Time",
            "current_artist": "Pink Floyd",
            "is_playing": True,
        },
        "turns": [
            # Save queue: name collision with "Chill Vibe" -> appends UTC epoch!
            {"text": "save queue as playlist Chill Vibe"},
            # Save queue with fresh name (slices from current_index=1, skips empty path)
            {"text": "save queue as playlist Night Drive"},
            # Move Track 4 (Paranoid Android) after current_index
            {"text": "move Paranoid Android to top"},
            # Move Track 1 (above current_index) -> target < cur branch
            {"text": "move Track 1 to top"},
            # Move current track itself (no-op move_queue_item)
            {"text": "move Time to top"},
            # Move missing
            {"text": "move Missing Song to top"},
            # Remove by number ("2"), last, first, title, missing, and superscript "²"
            {"text": "remove track 2 from queue"},
            {"text": "remove the last song"},
            {"text": "remove the first song"},
            {"text": "remove Money"},
            {"text": "remove Ghost"},
            {"text": "remove ²"},
        ],
    },
    {
        "name": "queue_edit_empty_and_short",
        "seed": 506,
        "clock": [2026, 11, 24, 12, 0, 0],
        "library": BASE_LIB,
        "engine": {"queue": [Q3[0]], "current_index": 0},
        "turns": [
            {"text": "move Track 1 to top"},
            {"text": "save walk"},
            {"text": "clear queue"},
            {"text": "remove track 1 from queue"},
            {"text": "save queue as playlist Empty"},
        ],
    },
    {
        "name": "moods_and_track_info",
        "seed": 606,
        "clock": [2026, 11, 24, 15, 0, 0],
        "library": BASE_LIB,
        "engine": {
            "queue": Q3,
            "current_index": 0,
            "current_path": "/m/echoes_pf.flac",
            "current_track": "Echoes",
            "current_artist": "Pink Floyd",
            "current_album": "Meddle",
            "duration": 1412.0,
        },
        "turns": [
            # Track info from DB row (has _timbre -> must strip timbre)
            {"text": "tell me about this track"},
            # Moods: known profiles + unknown profile
            {"text": "play something chill"},
            {"text": "play something heavy"},
            {"text": "play something rainy"},
            {"text": "play something cosmic"},
        ],
    },
    {
        "name": "track_info_fallbacks",
        "seed": 607,
        "clock": [2026, 11, 24, 15, 0, 0],
        "library": BASE_LIB,
        "engine": {
            "queue": [],
            "current_index": 0,
            "current_path": "/m/not_in_db.flac",
            "current_track": "Live Bootleg",
            "current_artist": "Mystery Band",
            "current_album": "",
            "duration": 61.0,
        },
        "turns": [
            {"text": "tell me about this track"},
        ],
    },
    {
        "name": "track_info_queue_row_and_nothing",
        "seed": 608,
        "clock": [2026, 11, 24, 15, 0, 0],
        "library": BASE_LIB,
        "engine": {
            "queue": [{"path": "", "track_title": "Queue Only", "artist_name": "Band Q", "album_title": "Alb Q", "duration": " 125.0 "}],
            "current_index": 0,
            "current_path": "",
            "current_track": "",
        },
        "turns": [
            {"text": "tell me about this track"},
            {"text": "clear queue"},
            {"text": "tell me about this track"},
        ],
    },
    {
        "name": "similar_and_more_by",
        "seed": 707,
        "clock": [2026, 11, 24, 16, 0, 0],
        "library": BASE_LIB,
        "engine": {
            "queue": [Q3[0]],
            "current_index": 0,
            "current_path": "/m/echoes_pf.flac",
            "current_track": "Echoes",
            "current_artist": "Pink Floyd",
        },
        "turns": [
            # Queue similar songs (verb: queue, non-empty queue -> queue_last_many)
            {"text": "queue similar songs"},
            # Play similar (replaces queue, deferred_play=True; avoid now includes previous walk!)
            {"text": "play similar"},
            # More by this artist (caps at 5, respects avoid set)
            {"text": "play more by this artist"},
        ],
    },
    {
        "name": "similar_and_more_by_edge_cases",
        "seed": 708,
        "clock": [2026, 11, 24, 16, 0, 0],
        "library": BASE_LIB,
        "engine": {
            "queue": [],
            "current_index": 0,
            "current_path": "/m/single_no_album.mp3",
            "current_track": "Solo Sketch",
            "current_artist": "",
        },
        "turns": [
            # Unanalysed seed
            {"text": "play similar"},
            # No artist neighbors
            {"text": "play more by this artist"},
            # Empty walk
            {"text": "play Time"},
            {"text": "play similar"},
            # No seed at all
            {"text": "clear queue"},
            {"text": "play similar"},
            {"text": "play more by this artist"},
        ],
    },
    {
        "name": "anaphora_direct_and_implicit",
        "seed": 808,
        "clock": [2026, 11, 24, 18, 0, 0],
        "library": BASE_LIB,
        "engine": {"queue": [], "current_path": ""},
        "turns": [
            # Track pronoun ("play it") -> resolves to track entity, announces prefix
            {
                "text": "play it",
                "history": [
                    {"sender": "assistant", "text": "Found it.", "entities": {
                        "track": CATALOG[4], "artist": "Radiohead", "album": "OK Computer",
                        "intent": "play_now",
                    }},
                    {"sender": "user", "text": "play it"},
                ],
            },
            # Artist pronoun ("play them")
            {
                "text": "play them",
                "history": [
                    {"sender": "assistant", "text": "Here is Radiohead.", "entities": {
                        "artist": "Radiohead", "intent": "search_artist",
                    }},
                ],
            },
            # Album pronoun ("queue the album") -> pool has album, _apply returns None
            {
                "text": "add the album to queue",
                "history": [
                    {"sender": "assistant", "text": "OK Computer.", "entities": {
                        "album": "OK Computer", "intent": "search_album",
                    }},
                ],
            },
            # Multi pronoun ("play those") -> pool empty
            {
                "text": "play those",
                "history": [
                    {"sender": "assistant", "text": "Some tracks.", "entities": {
                        "track": CATALOG[4], "intent": "play_now",
                    }},
                ],
            },
            # Implicit play_similar with no current_path -> resolves track + tail announce
            {"text": "clear queue"},
            {
                "text": "play similar",
                "history": [
                    {"sender": "assistant", "text": "Playing Paranoid Android.", "entities": {
                        "track": CATALOG[4], "artist": "Radiohead", "intent": "play_now",
                    }},
                ],
            },
            # Implicit play_more_by with no current_path -> resolves artist via NOCASE SQL + tail announce
            {"text": "clear queue"},
            {
                "text": "play more by this artist",
                "history": [
                    {"sender": "assistant", "text": "Pink Floyd is great.", "entities": {
                        "artist": "pink floyd", "intent": "search_artist",
                    }},
                ],
            },
            # Legacy bold markdown: "Title — Artist"
            {
                "text": "play that song",
                "history": [
                    {"sender": "assistant", "text": "Queued **Karma Police — Radiohead**."},
                ],
            },
            # Legacy bold markdown: artist only (single bold that doesn't match track title? or artist trigger)
            {
                "text": "play them",
                "history": [
                    {"sender": "assistant", "text": "Check out **TitleX** and **Radiohead**."},
                ],
            },
        ],
    },
    {
        "name": "anaphora_ambiguity_and_far_back",
        "seed": 809,
        "clock": [2026, 11, 24, 18, 0, 0],
        "library": BASE_LIB,
        "engine": {"queue": [], "current_path": ""},
        "turns": [
            # Ambiguity (2 tracks within rank gap <= 2) -> user says "yes"
            {
                "text": "play it",
                "history": [
                    {"sender": "assistant", "text": "A", "entities": {"track": CATALOG[2], "intent": "play_now"}},
                    {"sender": "assistant", "text": "B", "entities": {"track": CATALOG[4], "intent": "play_now"}},
                ],
            },
            {"text": "yes"},
            # Ambiguity -> user says "no" -> switches to runner-up alt_intent!
            {
                "text": "play it",
                "history": [
                    {"sender": "assistant", "text": "A", "entities": {"track": CATALOG[2], "intent": "play_now"}},
                    {"sender": "assistant", "text": "B", "entities": {"track": CATALOG[4], "intent": "play_now"}},
                ],
            },
            {"text": "no"},
            # Active intent weight (1.0) vs passive search (0.5): rank 2 play_now beats rank 0 search_track!
            {
                "text": "play it",
                "history": [
                    {"sender": "assistant", "text": "Old play", "entities": {"track": CATALOG[2], "intent": "play_now"}},
                    {"sender": "user", "text": "u1"},
                    {"sender": "assistant", "text": "Recent search", "entities": {"track": CATALOG[4], "intent": "search_track"}},
                ],
            },
            {"text": "yes"},
            # Far-back confirmation (rank > 12) -> "Just to confirm, sir..." -> "yes"
            {
                "text": "play it",
                "history": _far_history(
                    {"sender": "assistant", "text": "Way back", "entities": {"track": CATALOG[4], "intent": "play_now"}},
                    pad_pairs=7,
                ),
            },
            {"text": "yes"},
            # Far-back confirmation -> "no"
            {
                "text": "play it",
                "history": _far_history(
                    {"sender": "assistant", "text": "Way back", "entities": {"track": CATALOG[4], "intent": "play_now"}},
                    pad_pairs=7,
                ),
            },
            {"text": "no"},
        ],
    },
]


def _serialize_pending(p):
    if p is None:
        return None
    return {
        "prompt": p.prompt,
        "on_yes_action": p.on_yes_action,
        "on_yes_msg": p.on_yes_msg,
        "on_no_msg": p.on_no_msg,
        "has_on_yes": p.on_yes_callback is not None,
        "has_on_no": p.on_no_callback is not None,
    }


def _serialize_choice(c):
    if c is None:
        return None
    return {
        "prompt": c.prompt,
        "options": [
            {"id": o.id, "title": o.title, "subtitle": o.subtitle}
            for o in c.options
        ],
    }


async def _run_scenario(sc: dict) -> dict:
    calls: list = []
    lib = _FakeLibrary(sc["library"], calls)
    eng = _FakeEngine(sc.get("engine", {}), calls)
    runner = ar.AssistantRunner(lib, eng)
    runner._dispatch_llm = runner._execute_intent

    orig_random = ar.random
    orig_tg = ar.track_graph
    orig_dt_mod = sys.modules["datetime"]
    orig_time_fn = real_time.time

    ar.random = _ScriptedRandom(sc["seed"])
    ar.track_graph = _fake_track_graph(lib, calls)

    turn_results = []
    try:
        for turn in sc["turns"]:
            calls.clear()
            clk = turn.get("clock", sc["clock"])
            dt_mod, epoch = _make_datetime_shim(clk)
            sys.modules["datetime"] = dt_mod
            real_time.time = lambda e=epoch: e
            hist = turn.get("history", [])
            resp = await runner.dispatch_text(
                turn["text"],
                history_provider=lambda h=hist: h,
            )
            intent = resp.intent
            turn_results.append({
                "spoken": resp.spoken,
                "displayed": resp.displayed,
                "success": resp.success,
                "extras": resp.extras,
                "action": resp.action,
                "deferred_play": resp.deferred_play,
                "intent": {
                    "name": intent.name,
                    "query": intent.query,
                    "raw": intent.raw,
                    "extras": intent.extras,
                } if intent else None,
                "calls": copy.deepcopy(calls),
                "pending": _serialize_pending(runner._pending),
                "choice": _serialize_choice(runner._pending_choice),
                "recent": list(runner._recent),
                "queue": [t.get("path") for t in eng.queue],
                "current_index": eng.current_index,
                "is_playing": eng.is_playing,
                "is_shuffle": eng.is_shuffle,
            })
    finally:
        ar.random = orig_random
        ar.track_graph = orig_tg
        sys.modules["datetime"] = orig_dt_mod
        real_time.time = orig_time_fn

    return {
        "name": sc["name"],
        "seed": sc["seed"],
        "clock": sc["clock"],
        "library": sc["library"],
        "engine": sc.get("engine", {}),
        "turns": sc["turns"],
        "expected": turn_results,
    }


DIFFLIB_CASES = [
    ("", ""),
    ("", "abc"),
    ("abc", ""),
    ("abc", "abc"),
    ("abc", "xyz"),
    ("paranoid androyd", "paranoid android"),
    ("radiohed", "radiohead"),
    ("pink floyd", "pink floyd"),
    ("echoes", "echoes pink floyd"),
    ("αγάπη μου", "αγάπη"),
    ("björk", "bjork"),
    ("🎵 music 🎶", "🎵 music"),
    ("abracadabra", "cadabraabra"),
    ("aaaaaa", "aaa"),
    ("abcdef", "fedcba"),
    ("the dark side of the moon", "dark side moon"),
    ("comfortably numb", "comfortably numb pink floyd"),
    ("private thread", "privateThread"),
    ("12345", "1234567890"),
    ("x" * 20, "y" * 20),
    # Autojunk boundary: len(b) == 199 vs 200 with popular characters (> n//100 + 1)
    ("a" * 10 + "xyz", "a" * 196 + "xyz"),
    ("a" * 10 + "xyz", "a" * 197 + "xyz"),
    ("xyz" + "a" * 50, "xyz" + "a" * 197),
    ("b" * 5 + "a" * 100 + "c" * 5, "b" * 5 + "a" * 200 + "c" * 5),
    ("abcdefg" * 30, "abcdefg" * 30),
    ("a" * 200, "a" * 200),
    ("a" * 199 + "z", "a" * 199 + "z"),
    ("z" + "a" * 200, "z" + "a" * 200),
    ("m" + "a" * 200 + "n", "m" + "a" * 150 + "x" + "a" * 50 + "n"),
    ("ab" * 120, "ba" * 120),
    ("hello world " * 20, "hello brave world " * 20),
    ("q" + " " * 220 + "w", "q" + " " * 220 + "w"),
    ("αβγδεζηθ" * 25, "αβγδεζηθ" * 26),
    ("sigur rós", "sigur ros"),
    ("istanbul", "i̇stanbul"),
    ("ßeta", "sseta"),
    ("a\nb\nc", "a\r\nb\r\nc"),
    ("single", "double"),
    ("almost same string here", "almost same string ther"),
    ("short", "a much longer string that contains short inside it"),
]


def isdigit_hashes() -> list[int]:
    out = []
    h = 0x811C9DC5
    for o in range(sys.maxunicode + 1):
        if not (0xD800 <= o <= 0xDFFF):
            b = 1 if chr(o).isdigit() else 0
            h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
        if o % 4096 == 4095:
            out.append(h)
            h = 0x811C9DC5
    return out


CAP_CASES = [
    "", "chill", "CHILL", "cHiLl mix", "οδος", "ΟΔΟΣ", "ΟΣ", "Σ",
    "istanbul", "İstanbul", "ıstanbul", "ßeta", "123abc", "  chill", "🎵 music",
]


async def _build_all() -> dict:
    scenarios = [await _run_scenario(sc) for sc in SCENARIOS]
    return {
        "phrases": ar.AssistantRunner.JARVIS_PHRASES,
        "difflib": [
            [a, b, difflib.SequenceMatcher(None, a, b).ratio()]
            for a, b in DIFFLIB_CASES
        ],
        "isdigit_hashes": isdigit_hashes(),
        "capitalize": [[s, s.capitalize()] for s in CAP_CASES],
        "scenarios": scenarios,
    }


def main():
    data = asyncio.run(_build_all())
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    n_turns = sum(len(s["turns"]) for s in data["scenarios"])
    print(
        f"{OUT}: {len(data['scenarios'])} scenarios ({n_turns} turns), "
        f"{len(data['difflib'])} difflib cases"
    )


if __name__ == "__main__":
    main()
