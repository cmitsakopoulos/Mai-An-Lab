#!/usr/bin/env python3
"""Golden values for phase 5 stage d (PORT_MAP rule 3): Flet's
`AssistantRunner._dispatch_llm` loop and `llm_tools.execute_tool` against a
real synthetic SQLite database (`DatabaseManager` + `track_graph`) and a
local HTTP recording server.

Records every HTTP request (method, path, headers, body byte-for-byte), every
tool result dict, and every turn's response/state so
`test/assistant_agent_test.dart` replays the exact same fixtures and scripted
LLM replies against the port and checks byte-for-byte equality.

    test/goldens/assistant_agent.json   (committed; no personal data)

Usage (from mai_an_flutter/):
    ~/miniconda3/bin/python tool/export_agent_goldens.py
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import random as _random
import struct
import sys
import tempfile
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.path.dirname(HERE)
REPO = os.path.dirname(PORT)
sys.path.insert(0, os.path.join(REPO, "StreamripApp"))

from utils import assistant_runner as ar  # noqa: E402
from utils import db_manager as dbm  # noqa: E402
from utils import llm_engine as le  # noqa: E402
from utils import llm_tools  # noqa: E402
from utils import metadata_ai  # noqa: E402
from utils import track_graph as tg  # noqa: E402
from utils.config import AssistantConfig  # noqa: E402

logging.disable(logging.CRITICAL)

OUT = os.path.join(PORT, "test", "goldens", "assistant_agent.json")
JSON_CT = "application/json; charset=UTF-8"
KEEP_HEADERS = ("authorization", "content-type", "x-goog-api-key")


# ── Deterministic 64-bit LCG random (same formula as export_runner_goldens) ──

class ScriptedRandom:
    def __init__(self, seed: int):
        self.state = seed & ((1 << 64) - 1)

    def _next(self) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        return self.state >> 32

    def randbelow(self, n: int) -> int:
        return self._next() % n

    def random(self) -> float:
        return self._next() / 4294967296.0

    def choice(self, seq):
        return seq[self.randbelow(len(seq))]

    def randint(self, a: int, b: int) -> int:
        return a + self.randbelow(b - a + 1)

    def shuffle(self, seq) -> None:
        for i in range(len(seq) - 1, 0, -1):
            j = self.randbelow(i + 1)
            seq[i], seq[j] = seq[j], seq[i]


# ── Local HTTP recording server ──────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.replies: list[dict] = []
        self.requests: list[dict] = []


REC = Recorder()


class Handler(BaseHTTPRequestHandler):
    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        REC.requests.append({
            "method": self.command,
            "path": self.path,
            "headers": {
                k: self.headers[k]
                for k in KEEP_HEADERS
                if self.headers.get(k) is not None
            },
            "body": body,
        })
        r = REC.replies.pop(0)
        data = r["body"].encode("utf-8")
        self.send_response(r["status"])
        self.send_header("Content-Type", r.get("content_type", JSON_CT))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *a):
        pass


def reply(obj, status=200, content_type=JSON_CT) -> dict:
    return {
        "status": status,
        "content_type": content_type,
        "body": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False),
    }


def msg_reply(content="", tool_calls=None, extra_msg=None, include_role=True) -> dict:
    m: dict = {}
    if include_role:
        m["role"] = "assistant"
    m["content"] = content
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    if extra_msg:
        m.update(extra_msg)
    return reply({"choices": [{"message": m}]})


def tc(call_id: str, name: str, args: dict, extra_fn: dict | None = None) -> dict:
    fn = {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
    if extra_fn:
        fn.update(extra_fn)
    return {"id": call_id, "type": "function", "function": fn}


# ── Synthetic SQLite fixture ─────────────────────────────────────────────────

# (id, name, album_count, track_count)
ARTISTS = [
    (1, "Pink Floyd", 2, 3),
    (2, "Radiohead", 1, 2),
    (3, "Young the Giant", 1, 1),
    (4, "Sigur Rós", 1, 1),
    (5, "Αλέξης", 1, 1),
    (6, "Mystery Band", 1, 1),
]

# (id, artist_id, title, year, genre, track_count)
ALBUMS = [
    (1, 1, "Meddle", 1971, "Progressive Rock", 1),
    (2, 1, "The Dark Side of the Moon", 1973, "Rock", 2),
    (3, 2, "OK Computer", 1997, "Rock/Alt, Électronique, Divers", 2),
    (4, 3, "Home of the Strange", 2016, "Indie Rock", 1),
    (5, 4, "Ágætis byrjun", 1999, "Post-Rock", 1),
    (6, 5, "Μόνος", 2020, "Jazz", 1),
    (7, 6, "Untitled", 2022, "", 1),
]

# (id, album_id, title, path, duration, format, track_num, added_date, bpm, energy, brightness)
TRACKS = [
    (1, 1, "Echoes", "/m/echoes_pf.flac", 1412.0, "flac", 6, 1000.0, 68.0, 0.35, 0.3),
    (2, 2, "Time", "/m/time_pf.flac", 413.0, "flac", 4, 1010.0, 120.0, 0.55, 0.5),
    (3, 2, "Money", "/m/money_pf.flac", 382.0, "flac", 6, 1020.0, 124.0, 0.65, 0.6),
    (4, 3, "Paranoid Android", "/m/paranoid.flac", 387.0, "flac", 2, 1030.0, 84.0, 0.85, 0.75),
    (5, 3, "Karma Police", "/m/karma.flac", 264.0, "flac", 6, 1040.0, 75.0, 0.4, 0.4),
    (6, 4, "Echoes", "/m/echoes_ytg.flac", 225.0, "mp3", 1, 1050.0, 122.0, 0.72, 0.65),
    (7, 5, "Svefn-g-englar", "/m/svefn.flac", 604.0, "flac", 2, 1060.0, 70.0, 0.25, 0.2),
    (8, 6, "Νύχτα", "/m/nyxta.mp3", 195.0, "mp3", 1, 1070.0, 0.0, 0.0, 0.1),
    (9, 7, "Ghost", "/m/ghost.flac", 210.0, "flac", 1, 1080.0, 140.0, 0.92, 0.85),
]

# (path, count, last_played, features_version, timbre_bytes_list_or_None, pca_floats_or_None)
PLAY_COUNTS = [
    ("/m/echoes_pf.flac", 12, "2026-11-20 10:00:00", 5, [1, 2, 3, 4], [1.0, 0.0, 0.0, 0.0]),
    ("/m/time_pf.flac", 12, "2026-11-22 10:00:00", 5, [5, 6, 7, 8], [0.9, 0.4, 0.0, 0.0]),
    ("/m/money_pf.flac", 5, "2026-11-21 10:00:00", 5, [9, 10, 11, 12], [0.8, 0.5, 0.1, 0.0]),
    ("/m/paranoid.flac", 8, "2026-11-19 10:00:00", 5, [13, 14, 15, 16], [0.6, 0.8, 0.0, 0.0]),
    ("/m/svefn.flac", 3, "2026-11-17 10:00:00", 5, [17, 18, 19, 20], [0.2, 0.9, 0.3, 0.0]),
]

# (id, name, created, color)
PLAYLISTS = [
    (1, "Late Night", 1700000100, "#FF9F0A"),
    (2, "Empty List", 1700000200, "#0A84FF"),
]

# (playlist_id, track_path, order_index)
PLAYLIST_TRACKS = [
    (1, "/m/echoes_pf.flac", 0),
    (1, "/m/svefn.flac", 1),
]

# (track_path, played_at_fixed, event, seed_path)
HISTORY = [
    ("/m/echoes_pf.flac", 1700000500, "played", None),
    ("/m/time_pf.flac", 1700000400, "completed", None),
    ("/m/paranoid.flac", 1700000600, "played", None),
    ("/m/karma.flac", 1700000700, "skipped_early", None),
]

# (artist_name, country, genres_json, source, provenance_json)
ENRICHMENT = [
    ("Pink Floyd", "GB", json.dumps([{"name": "progressive rock"}, {"name": "psychedelic rock"}]),
     "musicbrainz", json.dumps({"country": "musicbrainz", "genres": "musicbrainz"})),
    ("Radiohead", "GB", json.dumps([{"name": "alternative rock"}]),
     "musicbrainz", json.dumps({"country": "musicbrainz", "genres": "musicbrainz"})),
    ("Sigur Rós", "IS", json.dumps([]), "musicbrainz", json.dumps({"country": "musicbrainz"})),
]

# (artist_name, payload_json, state, created_at)
AI_RESEARCH = [
    ("Radiohead", json.dumps({
        "status": "ok", "genres": ["art rock", "electronic"],
        "country": "GB", "summary": "English rock band from Abingdon.", "confidence": "high",
    }), "pending", 1700000000.0),
    ("Mystery Band", json.dumps({
        "status": "ok", "genres": ["ambient"], "country": "DE",
        "summary": "Ambient duo.", "confidence": "medium",
    }), "pending", 1700000010.0),
]

FIXTURE = {
    "artists": ARTISTS,
    "albums": ALBUMS,
    "tracks": TRACKS,
    "play_counts": PLAY_COUNTS,
    "playlists": PLAYLISTS,
    "playlist_tracks": PLAYLIST_TRACKS,
    "history": HISTORY,
    "enrichment": ENRICHMENT,
    "ai_research": AI_RESEARCH,
}


class FakeEngine:
    def __init__(self, spec: dict | None = None):
        spec = spec or {}
        self.queue: list[dict] = [dict(t) for t in spec.get("queue", [])]
        self.current_index: int = spec.get("current_index", 0)
        self.is_playing: bool = spec.get("is_playing", False)
        self.is_shuffle: bool = spec.get("is_shuffle", False)
        self.current_path: str = spec.get("current_path", "")
        self.current_track: str = spec.get("current_track", "")
        self.current_artist: str = spec.get("current_artist", "")
        self.current_album: str = spec.get("current_album", "")
        self.duration: float = spec.get("duration", 0.0)
        self.calls: list = []

    def _sync(self):
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

    def set_queue(self, tracks, start_index=0):
        self.calls.append(["set_queue", [t.get("path") for t in tracks], start_index])
        self.queue = [dict(t) for t in tracks]
        self.current_index = start_index
        self._sync()

    def queue_last(self, track):
        self.calls.append(["queue_last", track.get("path")])
        self.queue.append(dict(track))

    def queue_next(self, track):
        self.calls.append(["queue_next", track.get("path")])
        pos = min(self.current_index + 1, len(self.queue))
        self.queue.insert(pos, dict(track))

    def queue_last_many(self, tracks):
        self.calls.append(["queue_last_many", [t.get("path") for t in tracks]])
        self.queue.extend(dict(t) for t in tracks)

    def remove_from_queue(self, idx):
        self.calls.append(["remove_from_queue", idx])
        self.queue.pop(idx)
        if idx < self.current_index:
            self.current_index -= 1
        elif self.current_index >= len(self.queue):
            self.current_index = max(0, len(self.queue) - 1)
        self._sync()

    def move_queue_item(self, old_idx, new_idx):
        self.calls.append(["move_queue_item", old_idx, new_idx])
        item = self.queue.pop(old_idx)
        self.queue.insert(new_idx, item)
        self._sync()

    def next(self):
        self.calls.append(["next"])
        if self.current_index + 1 < len(self.queue):
            self.current_index += 1
            self._sync()

    def previous(self):
        self.calls.append(["previous"])
        if self.current_index > 0:
            self.current_index -= 1
            self._sync()

    def pause(self):
        self.calls.append(["pause"])
        self.is_playing = False

    def play(self):
        self.calls.append(["play"])
        self.is_playing = True

    def stop(self):
        self.calls.append(["stop"])
        self.is_playing = False
        self.current_path = ""
        self.current_track = ""
        self.current_artist = ""
        self.current_album = ""

    def clear_queue(self):
        self.calls.append(["clear_queue"])
        self.queue.clear()
        self.current_index = 0
        self.is_playing = False
        self._sync()


async def build_db() -> tuple[dbm.DatabaseManager, str]:
    tmp = tempfile.mkdtemp(prefix="mai_an_agent_db_")
    db_path = os.path.join(tmp, "library.db")
    db = dbm.DatabaseManager(db_path)
    await db.initialize()
    await db.close()
    db = dbm.DatabaseManager(db_path)
    await db.initialize()
    conn = await db.get_connection()

    for aid, name, alc, tc_n in ARTISTS:
        await conn.execute(
            "INSERT INTO artists (id, name, album_count, track_count) VALUES (?, ?, ?, ?)",
            (aid, name, alc, tc_n),
        )
    for alid, aid, title, year, genre, tc_n in ALBUMS:
        await conn.execute(
            "INSERT INTO albums (id, artist_id, title, year, genre, track_count) VALUES (?, ?, ?, ?, ?, ?)",
            (alid, aid, title, year, genre, tc_n),
        )
    for tid, alid, title, path, dur, fmt, tnum, added, bpm, energy, bright in TRACKS:
        await conn.execute(
            "INSERT INTO tracks (id, album_id, title, path, duration, format, track_num, added_date, bpm, energy, brightness) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, alid, title, path, dur, fmt, tnum, added, bpm, energy, bright),
        )
    for path, cnt, lp, fv, timbre, coords in PLAY_COUNTS:
        tb = bytes(timbre) if timbre is not None else None
        cb = struct.pack(f"<{len(coords)}f", *coords) if coords is not None else None
        await conn.execute(
            "INSERT INTO play_counts (track_path, count, last_played, features_version, timbre, pca_coords) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (path, cnt, lp, fv, tb, cb),
        )
    for pid, name, created, color in PLAYLISTS:
        await conn.execute(
            "INSERT INTO playlists (id, name, created, color) VALUES (?, ?, ?, ?)",
            (pid, name, created, color),
        )
    for pid, path, oidx in PLAYLIST_TRACKS:
        await conn.execute(
            "INSERT INTO playlist_tracks (playlist_id, track_path, order_index) VALUES (?, ?, ?)",
            (pid, path, oidx),
        )
    for path, played_at, ev, seed in HISTORY:
        await conn.execute(
            "INSERT INTO playback_history (track_path, played_at, event, seed_path) VALUES (?, ?, ?, ?)",
            (path, played_at, ev, seed),
        )
    for aname, country, genres, source, prov in ENRICHMENT:
        await conn.execute(
            "INSERT INTO artist_enrichment (artist_name, country, genres, source, provenance) VALUES (?, ?, ?, ?, ?)",
            (aname, country, genres, source, prov),
        )
    for aname, payload, state, created in AI_RESEARCH:
        await conn.execute(
            "INSERT INTO artist_ai_research (artist_name, payload, state, created_at) VALUES (?, ?, ?, ?)",
            (aname, payload, state, created),
        )
    await conn.commit()
    tg.invalidate_coord_graph_cache()
    return db, tmp


def serialize_pending(p):
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


def serialize_choice(c):
    if c is None:
        return None
    return {
        "prompt": c.prompt,
        "options": [{"id": o.id, "title": o.title, "subtitle": o.subtitle} for o in c.options],
    }


def serialize_response(resp: ar.AssistantResponse, runner: ar.AssistantRunner, eng: FakeEngine) -> dict:
    return {
        "spoken": resp.spoken,
        "displayed": resp.displayed,
        "success": resp.success,
        "extras": resp.extras,
        "action": resp.action,
        "deferred_play": resp.deferred_play,
        "pending": serialize_pending(runner._pending),
        "choice": serialize_choice(runner._pending_choice),
        "queue": [t.get("path") for t in eng.queue],
        "current_index": eng.current_index,
        "is_playing": eng.is_playing,
        "is_shuffle": eng.is_shuffle,
        "engine_calls": list(eng.calls),
    }


# ── Direct execute_tool cases ────────────────────────────────────────────────

TOOL_CASES = [
    {"name": "search_library_query", "tool": "search_library", "args": {"query": "pink", "limit": 5}},
    {"name": "search_library_genre_only", "tool": "search_library", "args": {"genre": "rock", "limit": 2}},
    {"name": "search_library_query_and_genre", "tool": "search_library", "args": {"query": "pink", "genre": "progressive"}},
    {"name": "search_library_all_default", "tool": "search_library", "args": {}},
    {"name": "play_music_empty_args", "tool": "play_music", "args": {}},
    {"name": "play_music_empty_queue", "tool": "play_music", "args": {"query": "Time"}},
    {"name": "play_music_by_path", "tool": "play_music", "args": {"path": "/m/echoes_pf.flac"}},
    {"name": "play_music_busy_queue", "tool": "play_music", "args": {"query": "Time"},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac", "track_title": "Echoes", "artist_name": "Pink Floyd"}]}},
    {"name": "play_music_busy_queue_by_path", "tool": "play_music", "args": {"path": "/m/karma.flac"},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac", "track_title": "Echoes", "artist_name": "Pink Floyd"}]}},
    {"name": "enqueue_music_query", "tool": "enqueue_music", "args": {"query": "Karma Police"},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac"}]}},
    {"name": "play_next_by_path", "tool": "play_next", "args": {"query": "Time", "path": "/m/time_pf.flac"},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac"}, {"path": "/m/money_pf.flac"}]}},
    {"name": "play_similar", "tool": "play_similar", "args": {},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac", "track_title": "Echoes", "artist_name": "Pink Floyd"}],
                "current_path": "/m/echoes_pf.flac", "current_track": "Echoes", "current_artist": "Pink Floyd"}},
    {"name": "steer_mood_melancholic", "tool": "steer_mood", "args": {"mood": "melancholic"}},
    {"name": "search_acoustic_high_energy", "tool": "search_by_acoustic_profile",
     "args": {"min_energy": 0.7, "min_bpm": 100}},
    {"name": "search_acoustic_zero_energy_and_bpm", "tool": "search_by_acoustic_profile",
     "args": {"max_energy": 0.1, "min_bpm": 100}},
    {"name": "search_acoustic_genre_brightness", "tool": "search_by_acoustic_profile",
     "args": {"max_brightness": 0.4, "genre": "rock"}},
    {"name": "playback_control_pause", "tool": "playback_control", "args": {"action": "pause"},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac"}], "is_playing": True}},
    {"name": "playback_control_bad", "tool": "playback_control", "args": {"action": "explode"}},
    {"name": "save_queue_as_playlist", "tool": "save_queue_as_playlist", "args": {"name": "Road Trip"},
     "engine": {"queue": [{"path": "/m/time_pf.flac"}, {"path": "/m/money_pf.flac"}], "current_index": 0}},
    {"name": "get_player_status_playing", "tool": "get_player_status", "args": {},
     "engine": {"queue": [{"path": "/m/time_pf.flac", "track_title": "Time", "artist_name": "Pink Floyd", "album_title": "The Dark Side of the Moon"}],
                "current_index": 0, "is_playing": True}},
    {"name": "get_player_status_empty", "tool": "get_player_status", "args": {}},
    {"name": "get_current_track_info", "tool": "get_current_track_info", "args": {},
     "engine": {"current_path": "/m/echoes_pf.flac"}},
    {"name": "get_library_overview", "tool": "get_library_overview", "args": {}},
    {"name": "get_top_played", "tool": "get_top_played", "args": {"limit": 3}},
    {"name": "list_artists_all", "tool": "list_artists", "args": {}},
    {"name": "list_artists_search_limit", "tool": "list_artists", "args": {"search": "pink", "limit": 5}},
    {"name": "list_artists_closest", "tool": "list_artists", "args": {"search": "radiohed"}},
    {"name": "get_artist_albums_ok", "tool": "get_artist_albums", "args": {"artist": "Pink Floyd"}},
    {"name": "get_artist_albums_empty", "tool": "get_artist_albums", "args": {}},
    {"name": "get_album_tracks_exact", "tool": "get_album_tracks", "args": {"album": "the dark side of the moon"}},
    {"name": "get_album_tracks_fuzzy", "tool": "get_album_tracks", "args": {"album": "compter"}},
    {"name": "get_album_tracks_missing", "tool": "get_album_tracks", "args": {"album": "zzzzzz"}},
    {"name": "list_playlists", "tool": "list_playlists", "args": {}},
    {"name": "get_playlist_tracks_ok", "tool": "get_playlist_tracks", "args": {"name": "late night"}},
    {"name": "get_playlist_tracks_missing", "tool": "get_playlist_tracks", "args": {"name": "nope"}},
    {"name": "get_recently_played", "tool": "get_recently_played", "args": {"limit": 2}},
    {"name": "get_track_details_path", "tool": "get_track_details", "args": {"path": "/m/echoes_pf.flac"}},
    {"name": "get_track_details_query", "tool": "get_track_details", "args": {"query": "paranoid"}},
    {"name": "get_track_details_current", "tool": "get_track_details", "args": {},
     "engine": {"current_path": "/m/nyxta.mp3"}},
    {"name": "get_track_details_missing", "tool": "get_track_details", "args": {"path": "/m/none.flac"}},
    {"name": "play_album_empty_queue", "tool": "play_album", "args": {"album": "The Dark Side of the Moon"}},
    {"name": "play_album_busy_queue", "tool": "play_album", "args": {"album": "OK Computer"},
     "engine": {"queue": [{"path": "/m/echoes_pf.flac"}]}},
    {"name": "play_album_missing", "tool": "play_album", "args": {"album": "Not Real"}},
    {"name": "play_playlist_empty_queue", "tool": "play_playlist", "args": {"name": "Late Night"}},
    {"name": "play_playlist_empty_list", "tool": "play_playlist", "args": {"name": "Empty List"}},
    {"name": "play_playlist_busy_queue", "tool": "play_playlist", "args": {"name": "Late Night"},
     "engine": {"queue": [{"path": "/m/time_pf.flac"}]}},
    {"name": "get_metadata_status", "tool": "get_metadata_status", "args": {}},
    {"name": "get_artist_metadata_with_ai", "tool": "get_artist_metadata", "args": {"artist": "radiohead"}},
    {"name": "get_artist_metadata_no_ai", "tool": "get_artist_metadata", "args": {"artist": "Pink Floyd"}},
    {"name": "get_artist_metadata_missing", "tool": "get_artist_metadata", "args": {"artist": "zzzzzz"}},
    {"name": "unknown_tool", "tool": "no_such_tool", "args": {}},
]


# ── Full _dispatch_llm loop scenarios ────────────────────────────────────────

LONG_HISTORY = [
    {"sender": "user" if i % 2 == 0 else "assistant", "text": f"msg {i}"}
    for i in range(12)
] + [{"sender": "user", "text": "find pink floyd and play echoes"}]

LOOP_SCENARIOS = [
    {
        "name": "multi_step_read_and_act_with_thought_signature",
        "seed": 101,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": "k-test", "gemini_model": "gemini-2.5-flash"},
        "engine": {"current_path": "/m/nyxta.mp3"},
        "turns": [
            {
                "text": "find pink floyd and play echoes",
                "history": LONG_HISTORY,
                "replies": [
                    msg_reply(
                        content="",
                        tool_calls=[
                            tc("call_1", "search_library", {"query": "pink floyd"},
                               extra_fn={"thought_signature": "SIG_ALPHA+/="}),
                            tc("call_2", "get_library_overview", {}),
                        ],
                    ),
                    msg_reply(
                        content="",
                        tool_calls=[
                            tc("call_3", "play_music", {"path": "/m/echoes_pf.flac"},
                               extra_fn={"thought_signature": "SIG_BETA"}),
                        ],
                    ),
                    msg_reply(content="Playing **Echoes** by Pink Floyd from your 9-track library, sir."),
                ],
            }
        ],
    },
    {
        "name": "custom_personality_ollama_no_role_raw_msg",
        "seed": 102,
        "config": {
            "llm_enabled": True, "llm_provider": "ollama",
            "ollama_endpoint": "/v1/", "ollama_model": "qwen2.5",
            "personality_prompt": "  You are DJ Retro, a cheerful 80s host.  ",
        },
        "engine": {},
        "turns": [
            {
                "text": "what is my metadata status and radiohead info",
                "history": [{"sender": "assistant", "text": "Welcome back!"}],
                "replies": [
                    msg_reply(
                        content="",
                        include_role=False,
                        tool_calls=[
                            tc("m1", "get_metadata_status", {}),
                            tc("m2", "get_artist_metadata", {"artist": "radiohead"}),
                        ],
                    ),
                    msg_reply(content="All set! **Radiohead** is tagged `alternative rock`."),
                ],
            }
        ],
    },
    {
        "name": "confirmation_interrupt_then_yes",
        "seed": 103,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": "k-test", "gemini_model": "gemini-2.5-flash"},
        "engine": {"queue": [{"path": "/m/echoes_pf.flac", "track_title": "Echoes", "artist_name": "Pink Floyd"}],
                   "current_path": "/m/echoes_pf.flac"},
        "turns": [
            {
                "text": "put on dark side of the moon",
                "history": [],
                "replies": [
                    msg_reply(
                        content="",
                        tool_calls=[tc("a1", "play_album", {"album": "The Dark Side of the Moon"})],
                    ),
                ],
            },
            {
                "text": "yes please",
                "history": [],
                "replies": [],
            },
        ],
    },
    {
        "name": "disambiguation_choice_interrupt_then_select",
        "seed": 104,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": "k-test", "gemini_model": "gemini-2.5-flash"},
        "engine": {},
        "turns": [
            {
                "text": "play echoes please",
                "history": [],
                "replies": [
                    msg_reply(
                        content="",
                        tool_calls=[tc("p1", "play_music", {"query": "Echoes"})],
                    ),
                ],
            },
            {
                "text": "1",
                "history": [],
                "replies": [],
            },
        ],
    },
    {
        "name": "max_steps_exhausted_final_no_tools",
        "seed": 105,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": "k-test", "gemini_model": "gemini-2.5-flash"},
        "engine": {},
        "turns": [
            {
                "text": "inspect everything in my library",
                "history": [],
                "replies": [
                    msg_reply(content="", tool_calls=[tc("s1", "list_artists", {})]),
                    msg_reply(content="", tool_calls=[tc("s2", "get_artist_albums", {"artist": "Pink Floyd"})]),
                    msg_reply(content="", tool_calls=[tc("s3", "get_album_tracks", {"album": "Meddle"})]),
                    msg_reply(content="", tool_calls=[tc("s4", "list_playlists", {})]),
                    msg_reply(content="", tool_calls=[tc("s5", "get_playlist_tracks", {"name": "Late Night"})]),
                    msg_reply(content="", tool_calls=[tc("s6", "get_recently_played", {"limit": 3})]),
                    msg_reply(content="Summary complete, sir."),
                ],
            }
        ],
    },
    {
        "name": "max_steps_exhausted_final_fails",
        "seed": 106,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": "k-test", "gemini_model": "gemini-2.5-flash"},
        "engine": {},
        "turns": [
            {
                "text": "loop forever",
                "history": [],
                "replies": [
                    msg_reply(content="", tool_calls=[tc(f"k{i}", "get_player_status", {})])
                    for i in range(6)
                ] + [reply("boom", status=500, content_type="text/plain")],
            }
        ],
    },
    {
        "name": "llm_disabled_runs_deterministic",
        "seed": 107,
        "config": {"llm_enabled": False, "llm_provider": "gemini", "gemini_api_key": "k-test"},
        "engine": {},
        "turns": [
            {"text": "play Time", "history": [], "replies": []},
        ],
    },
    {
        "name": "unconfigured_fallbacks",
        "seed": 108,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": ""},
        "engine": {},
        "turns": [
            {"text": "play Time", "history": [], "replies": []},
            {"text": "explain quantum mechanics", "history": [], "replies": []},
        ],
    },
    {
        "name": "http_error_fallbacks_and_fast_bypass",
        "seed": 109,
        "config": {"llm_enabled": True, "llm_provider": "gemini", "gemini_api_key": "k-test", "gemini_model": "gemini-2.5-flash"},
        "engine": {"queue": [{"path": "/m/echoes_pf.flac"}], "is_playing": True},
        "turns": [
            {"text": "pause", "history": [], "replies": []},
            {"text": "play Time", "history": [], "replies": [reply("overloaded", status=503, content_type="text/plain")]},
            {"text": "why is the sky blue", "history": [], "replies": [reply("overloaded", status=503, content_type="text/plain")]},
        ],
    },
]


async def run_all(base: str) -> dict:
    le._GEMINI_URL = base + "/v1beta/openai/chat/completions"
    metadata_ai.research_available = lambda: False

    # Fixed clock for any runner handler that calls datetime.now() or time.time()
    fixed_dt = datetime.datetime(2026, 11, 24, 14, 30, 0)
    orig_dt = datetime.datetime

    class _FixedDatetime(orig_dt):
        @classmethod
        def now(cls, tz=None):
            return fixed_dt

    datetime.datetime = _FixedDatetime  # type: ignore[misc]
    _time.time = lambda: 1700001000.0  # type: ignore[assignment]

    tool_results = []
    for case in TOOL_CASES:
        db, _ = await build_db()
        eng = FakeEngine(case.get("engine"))
        rng = ScriptedRandom(42)
        ar.random = rng  # type: ignore[assignment]
        runner = ar.AssistantRunner(db, eng)
        runner.agent_reset()
        res = await llm_tools.execute_tool(case["tool"], dict(case["args"]), runner)
        tool_results.append({
            "name": case["name"],
            "tool": case["tool"],
            "args": case["args"],
            "engine": case.get("engine") or {},
            "result": res,
            "deferred_play": runner._agent_deferred_play,
            "interrupt": None if runner._agent_interrupt is None else {
                "spoken": runner._agent_interrupt.spoken,
                "displayed": runner._agent_interrupt.displayed,
                "success": runner._agent_interrupt.success,
                "extras": runner._agent_interrupt.extras,
            },
            "pending": serialize_pending(runner._pending),
            "choice": serialize_choice(runner._pending_choice),
            "queue": [t.get("path") for t in eng.queue],
            "engine_calls": list(eng.calls),
        })
        await db.close()

    loop_results = []
    for sc in LOOP_SCENARIOS:
        db, _ = await build_db()
        eng = FakeEngine(sc.get("engine"))
        rng = ScriptedRandom(sc["seed"])
        ar.random = rng  # type: ignore[assignment]
        runner = ar.AssistantRunner(db, eng)

        cfg_dict = dict(sc["config"])
        if "ollama_endpoint" in cfg_dict:
            cfg_dict["ollama_endpoint"] = base + cfg_dict["ollama_endpoint"]
        acfg = AssistantConfig(**cfg_dict)

        class _WrapCfg:
            assistant = acfg

        runner._load_assistant_cfg = lambda _c=_WrapCfg(): _c  # type: ignore[method-assign]

        turn_outs = []
        for turn in sc["turns"]:
            REC.replies = [dict(r) for r in turn["replies"]]
            REC.requests = []
            eng.calls.clear()
            hist = list(turn.get("history") or [])
            resp = await runner.dispatch_text(turn["text"], history_provider=lambda _h=hist: _h)
            turn_outs.append({
                "requests": list(REC.requests),
                "response": serialize_response(resp, runner, eng),
            })
        loop_results.append({
            **sc,
            "expected": turn_outs,
        })
        await db.close()

    return {
        "fixture": FIXTURE,
        "tool_cases": tool_results,
        "loop_scenarios": loop_results,
    }


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        data = asyncio.run(run_all(base))
    finally:
        server.shutdown()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{OUT}: {len(data['tool_cases'])} tool cases, {len(data['loop_scenarios'])} loop scenarios")
    for s in data["loop_scenarios"]:
        reqs = sum(len(t["requests"]) for t in s["expected"])
        print(f"  {s['name']}: {len(s['turns'])} turn(s), {reqs} HTTP request(s)")


if __name__ == "__main__":
    main()
