"""
LLM tool definitions & execution dispatch for Jarvis (in-process AI agent).

Tools are a THIN BRIDGE onto AssistantRunner's already-correct intent handlers.
The LLM does reasoning/orchestration; execution reuses the exact logic the
regex and semantic paths use — path-keyed identity, non-destructive queue ops,
the DSP similarity walk, disambiguation prompts. This keeps agent behaviour
identical to the deterministic path and removes the entire "imagined API" bug
class the previous version shipped.

Schemas are plain JSON-Schema (lowercase types) so they work with every
OpenAI-compatible endpoint: Gemini's OpenAI shim, Ollama, and LM Studio.

Risk gating (destructive / outward actions) is delegated to the runner via
``agent_request_confirmation`` so the existing yes/no PendingConfirmation
machinery drives the UX — the agent never wipes a queue or downloads without
the user's say-so.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, TYPE_CHECKING

from utils import assistant_intent as ai

if TYPE_CHECKING:
    from utils.assistant_runner import AssistantRunner

logger = logging.getLogger(__name__)


# ── Markdown → speech-friendly plain text ────────────────────────────────────

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_EMPH = re.compile(r"(\*\*|__|\*|_|`)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[•\-\*]\s+", re.MULTILINE)
_WS = re.compile(r"[ \t]{2,}")


def strip_markdown(text: str) -> str:
    """Flatten markdown to plain prose so TTS doesn't read '**' or link URLs
    aloud. Keeps the words, drops the syntax."""
    if not text:
        return ""
    t = _MD_LINK.sub(r"\1", text)
    t = _MD_HEAD.sub("", t)
    t = _MD_BULLET.sub("", t)
    t = _MD_EMPH.sub("", t)
    t = _WS.sub(" ", t)
    return t.strip()


# ── DB row → JSON-safe, LLM-relevant projection ──────────────────────────────

_TRACK_FIELDS = ("path", "title", "artist", "album", "genre", "year",
                 "duration", "format", "bpm", "energy", "track_num", "count",
                 "last_played")


def _slim_track(d: Dict[str, Any]) -> Dict[str, Any]:
    """Project a db row to JSON-safe, useful fields. Drops the timbre BLOB and
    any other bytes so tool results (and chat history) serialise cleanly."""
    out: Dict[str, Any] = {}
    for k in _TRACK_FIELDS:
        v = d.get(k)
        if isinstance(v, (bytes, bytearray)) or v is None:
            continue
        out[k] = v
    return out


# ── Tool declarations (OpenAI-compatible JSON-Schema) ────────────────────────

def get_agent_tools() -> List[Dict[str, Any]]:
    """Structured tool declarations for the LLM. Lowercase JSON-Schema types
    for OpenAI-compatible function calling (Gemini shim / Ollama / LM Studio)."""
    return [
        {
            "name": "search_library",
            "description": (
                "Search the user's LOCAL music library for tracks. Returns matches "
                "with their 'path' (the stable identity you pass to other tools). "
                "Use this to check what the user owns before playing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Track title, artist, or album to search for."},
                    "genre": {"type": "string", "description": "Optional genre filter, e.g. 'Jazz'."},
                    "limit": {"type": "integer", "description": "Max results (default 10)."},
                },
                "required": [],
            },
        },
        {
            "name": "play_music",
            "description": (
                "Play tracks matching a query immediately, REPLACING the current queue. "
                "Resolves the query against the local library (handles artist/title/album). "
                "If a non-empty queue would be replaced, the user is asked to confirm first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to play (artist, title, or album)."},
                    "path": {"type": "string", "description": "Optional exact track path from search_library for a precise pick."},
                },
                "required": [],
            },
        },
        {
            "name": "enqueue_music",
            "description": "Add tracks matching a query to the END of the queue without interrupting playback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to enqueue."},
                    "path": {"type": "string", "description": "Optional exact track path from search_library."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "play_next",
            "description": "Insert a track to play right AFTER the current one, without clearing the queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to play next."},
                    "path": {"type": "string", "description": "Optional exact track path from search_library."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "play_similar",
            "description": (
                "Curate more tracks acoustically similar to what is currently playing, using the "
                "DSP similarity walk. Use when the user wants 'more like this'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "steer_mood",
            "description": (
                "Build a mood/vibe-based mix from the local library based on any mood descriptor "
                "(e.g. 'chill', 'energetic', 'melancholic', 'focus', 'workout', 'dark', 'upbeat', 'rainy day') "
                "and start playing it using DSP acoustic feature matching."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "description": "Target mood or vibe description."},
                },
                "required": ["mood"],
            },
        },
        {
            "name": "search_by_acoustic_profile",
            "description": (
                "Search the local music library by acoustic properties extracted by DSP "
                "(energy 0.0-1.0, brightness 0.0-1.0, bpm range, genre filters). "
                "Use when the user asks for music with specific acoustic traits or energy levels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_energy": {"type": "number", "description": "Minimum energy (0.0=calm to 1.0=intense)."},
                    "max_energy": {"type": "number", "description": "Maximum energy (0.0 to 1.0)."},
                    "min_brightness": {"type": "number", "description": "Minimum acoustic brightness (0.0=dark/warm to 1.0=bright)."},
                    "max_brightness": {"type": "number", "description": "Maximum acoustic brightness."},
                    "min_bpm": {"type": "integer", "description": "Minimum tempo in BPM."},
                    "max_bpm": {"type": "integer", "description": "Maximum tempo in BPM."},
                    "genre": {"type": "string", "description": "Optional genre substring filter."},
                    "limit": {"type": "integer", "description": "Max results (default 10)."},
                },
                "required": [],
            },
        },
        {
            "name": "playback_control",
            "description": "Control transport: pause, resume, skip, previous, stop, clear_queue, or shuffle.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "skip", "previous", "stop", "clear_queue", "shuffle"],
                        "description": "Transport action to perform.",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "save_queue_as_playlist",
            "description": "Save the current queue/walk (from the current position onward) as a named playlist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playlist name."},
                },
                "required": ["name"],
            },
        },
        {
            "name": "get_player_status",
            "description": "Get the currently playing track, playback state, and queue length.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_current_track_info",
            "description": "Get detailed metadata (title, artist, album, duration) about the current track.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "search_online",
            "description": (
                "Search Qobuz ONLINE for tracks/albums the user may not own locally. Returns results. "
                "Set download=true to fetch the top result — this ALWAYS asks the user to confirm first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Online search query."},
                    "download": {"type": "boolean", "description": "If true, offer to download the top match (requires user confirmation)."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_library_overview",
            "description": "Summary stats of the local library: total tracks, artist/album/playlist counts, and the top genres. Use for 'how big is my library' / 'what genres do I have'.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_top_played",
            "description": "The user's most-played tracks with play counts. Use for 'my most played', 'favourite songs'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many (default 10)."}},
                "required": [],
            },
        },
        {
            "name": "list_artists",
            "description": "List artists in the library (with per-artist track & album counts), optionally filtered. Use for 'how many artists', 'do I have <artist>', 'how many tracks by <artist>'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Optional name filter."},
                    "limit": {"type": "integer", "description": "Max results (default 25)."},
                },
                "required": [],
            },
        },
        {
            "name": "get_artist_albums",
            "description": "List the albums the library holds for a given artist (title, year, genre, track count).",
            "parameters": {
                "type": "object",
                "properties": {"artist": {"type": "string", "description": "Artist name."}},
                "required": ["artist"],
            },
        },
        {
            "name": "get_album_tracks",
            "description": "List the tracks on an album, in order. Artist is optional but disambiguates same-named albums.",
            "parameters": {
                "type": "object",
                "properties": {
                    "album": {"type": "string", "description": "Album title."},
                    "artist": {"type": "string", "description": "Optional artist name."},
                },
                "required": ["album"],
            },
        },
        {
            "name": "list_playlists",
            "description": "List the user's saved playlists with their track counts.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "get_playlist_tracks",
            "description": "List the tracks inside a named playlist, in order.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Playlist name."}},
                "required": ["name"],
            },
        },
        {
            "name": "get_recently_played",
            "description": "The most recently played tracks, newest first. Use for 'what did I just play', 'what was I listening to'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many (default 15)."}},
                "required": [],
            },
        },
        {
            "name": "get_track_details",
            "description": "Rich metadata for one track (album, genre, year, duration, bpm, energy). Pass a query, or a path, or nothing to describe the current track. Use for 'what bpm is this', 'what year is this from'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Track/artist to look up."},
                    "path": {"type": "string", "description": "Exact track path from another tool."},
                },
                "required": [],
            },
        },
        {
            "name": "play_album",
            "description": "Play a whole album in order, replacing the queue. Artist optional but disambiguates. Confirms first if a queue is already playing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "album": {"type": "string", "description": "Album title."},
                    "artist": {"type": "string", "description": "Optional artist name."},
                },
                "required": ["album"],
            },
        },
        {
            "name": "play_playlist",
            "description": "Play a named playlist in order, replacing the queue. Confirms first if a queue is already playing.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Playlist name."}},
                "required": ["name"],
            },
        },
    ]


# ── Execution dispatch ───────────────────────────────────────────────────────

async def execute_tool(tool_name: str, args: Dict[str, Any], runner: "AssistantRunner") -> Dict[str, Any]:
    """Dispatch one LLM function call. Bridges to the runner's proven handlers
    where possible; reads engine/DB state directly for status tools. Records the
    tool name for provenance and never raises — failures come back as a result
    dict the model can reason about."""
    logger.info("Jarvis agent tool: %s(%s)", tool_name, args)
    runner._agent_tools_used.append(tool_name)

    try:
        # ── Read-only library search ─────────────────────────────────────────
        if tool_name == "search_library":
            if runner.db is None:
                return {"success": False, "error": "Library unavailable."}
            query = (args.get("query") or "").strip()
            genre = (args.get("genre") or "").strip().lower()
            limit = int(args.get("limit") or 10)

            if query:
                rows = await runner.db.search_tracks_simple(query, limit=max(limit, 10))
            else:
                rows = await runner.db.get_all_tracks()

            out = []
            for r in (rows or []):
                d = dict(r)
                g = (d.get("genre") or "")
                if genre and genre not in g.lower():
                    continue
                out.append({
                    "path": d.get("path"),
                    "title": d.get("title") or d.get("track_title") or "Unknown",
                    "artist": d.get("artist") or d.get("artist_name") or "Unknown Artist",
                    "album": d.get("album") or d.get("album_title") or "",
                    "genre": g,
                })
                if len(out) >= limit:
                    break
            return {"success": True, "count": len(out), "tracks": out}

        # ── Playback: play (destructive → gated when queue non-empty) ─────────
        if tool_name == "play_music":
            query = (args.get("query") or "").strip()
            path = (args.get("path") or "").strip()
            extras = await _resolved_extras(runner, path)
            if not query and not extras:
                return {"success": False, "error": "Nothing to play — provide a query or path."}

            label = _play_label(extras, query)
            queue_busy = bool(getattr(runner.engine, "queue", None))
            if queue_busy:
                # Replacing an active queue is destructive: confirm first.
                async def _do_play():
                    return await runner._execute_intent(
                        ai.Intent(name=ai.INTENT_PLAY_NOW, query=query, raw=query, extras=extras)
                    )
                return runner.agent_request_confirmation(
                    spoken=f"That will replace your current queue with {label}, sir. Shall I proceed?",
                    displayed=f"This will **replace your current queue** with {label}. Proceed?",
                    on_yes=_do_play,
                )
            return await runner.agent_run_intent(ai.INTENT_PLAY_NOW, query=query, extras=extras)

        if tool_name == "enqueue_music":
            query = (args.get("query") or "").strip()
            extras = await _resolved_extras(runner, (args.get("path") or "").strip())
            return await runner.agent_run_intent(ai.INTENT_QUEUE_ADD, query=query, extras=extras)

        if tool_name == "play_next":
            query = (args.get("query") or "").strip()
            extras = await _resolved_extras(runner, (args.get("path") or "").strip())
            return await runner.agent_run_intent(ai.INTENT_QUEUE_NEXT, query=query, extras=extras)

        if tool_name == "play_similar":
            return await runner.agent_run_intent(ai.INTENT_PLAY_SIMILAR)

        if tool_name == "steer_mood":
            mood = (args.get("mood") or "chill").strip()
            return await runner.agent_run_intent(ai.INTENT_MOOD_STEER, query=mood)

        if tool_name == "search_by_acoustic_profile":
            if runner.db is None:
                return {"success": False, "error": "Library unavailable."}
            min_e = float(args.get("min_energy")) if args.get("min_energy") is not None else None
            max_e = float(args.get("max_energy")) if args.get("max_energy") is not None else None
            min_b = float(args.get("min_brightness")) if args.get("min_brightness") is not None else None
            max_b = float(args.get("max_brightness")) if args.get("max_brightness") is not None else None
            min_bpm = float(args.get("min_bpm")) if args.get("min_bpm") is not None else None
            max_bpm = float(args.get("max_bpm")) if args.get("max_bpm") is not None else None
            genre = (args.get("genre") or "").strip().lower()
            limit = int(args.get("limit") or 10)

            all_tracks = await runner.db.get_all_tracks()
            matches = []
            for t in (all_tracks or []):
                d = dict(t)
                g = (d.get("genre") or "").lower()
                if genre and genre not in g:
                    continue
                bpm = float(d.get("bpm") or d.get("tempo") or 0.0)
                if min_bpm is not None and bpm > 0 and bpm < min_bpm:
                    continue
                if max_bpm is not None and bpm > 0 and bpm > max_bpm:
                    continue
                energy = float(d.get("energy", 0.5) or 0.5)
                if min_e is not None and energy < min_e:
                    continue
                if max_e is not None and energy > max_e:
                    continue
                bright = float(d.get("brightness", 0.5) or 0.5)
                if min_b is not None and bright < min_b:
                    continue
                if max_b is not None and bright > max_b:
                    continue
                matches.append(_slim_track(d))
                if len(matches) >= limit:
                    break
            return {"success": True, "count": len(matches), "tracks": matches}

        if tool_name == "playback_control":
            action = (args.get("action") or "").strip().lower()
            mapping = {
                "pause": ai.INTENT_PAUSE, "resume": ai.INTENT_RESUME,
                "skip": ai.INTENT_SKIP, "next": ai.INTENT_SKIP,
                "previous": ai.INTENT_PREV, "prev": ai.INTENT_PREV,
                "stop": ai.INTENT_STOP, "clear_queue": ai.INTENT_CLEAR_QUEUE,
                "shuffle": ai.INTENT_SHUFFLE,
            }
            intent_name = mapping.get(action)
            if not intent_name:
                return {"success": False, "error": f"Unknown transport action: {action}"}
            return await runner.agent_run_intent(intent_name)

        if tool_name == "save_queue_as_playlist":
            name = (args.get("name") or "Saved Walk").strip()
            return await runner.agent_run_intent(ai.INTENT_SAVE_QUEUE, query=name)

        if tool_name == "get_current_track_info":
            return await runner.agent_run_intent(ai.INTENT_TRACK_INFO)

        # ── Read-only player status (direct engine read) ─────────────────────
        if tool_name == "get_player_status":
            eng = runner.engine
            if eng is None:
                return {"success": False, "error": "Engine unavailable."}
            queue = getattr(eng, "queue", []) or []
            idx = getattr(eng, "current_index", 0)
            current = None
            if 0 <= idx < len(queue):
                c = queue[idx]
                current = {
                    "title": c.get("track_title") or c.get("title", "Unknown"),
                    "artist": c.get("artist_name") or c.get("artist", "Unknown Artist"),
                    "album": c.get("album_title") or c.get("album", ""),
                }
            return {
                "success": True,
                "is_playing": bool(getattr(eng, "is_playing", False)),
                "currently_playing": current,
                "queue_length": len(queue),
            }

        # ── Online search / gated download ───────────────────────────────────
        if tool_name == "search_online":
            query = (args.get("query") or "").strip()
            want_download = bool(args.get("download"))
            if not query:
                return {"success": False, "error": "Empty online query."}

            results = await _search_qobuz(query)
            if isinstance(results, dict) and results.get("error"):
                return {"success": False, "error": results["error"]}
            if not results:
                return {"success": True, "results": [], "message": f"No online results for '{query}'."}

            slim = [
                {
                    "title": r.get("name"), "artist": r.get("artist"),
                    "type": r.get("media_type"), "url": r.get("url"),
                }
                for r in results[:5]
            ]

            if want_download and slim:
                top = slim[0]

                async def _do_download():
                    return await _download_item(top)

                return runner.agent_request_confirmation(
                    spoken=f"I found '{top['title']}' by {top['artist']} on Qobuz, sir. Shall I download it?",
                    displayed=f"Found **{top['title']}** — {top['artist']} on Qobuz. Download it?",
                    on_yes=_do_download,
                )
            return {"success": True, "results": slim}

        # ── Library knowledge (read-only) ────────────────────────────────────
        if tool_name == "get_library_overview":
            if runner.db is None:
                return {"success": False, "error": "Library unavailable."}
            total = await runner.db.get_total_tracks()
            artists = await runner.db.get_all_artists()
            albums = await runner.db.get_all_albums()
            playlists = await runner.db.get_all_playlists()
            genre_counts: Dict[str, int] = {}
            for row in (await runner.db.get_all_tracks() or []):
                g = (dict(row).get("genre") or "").strip()
                if g:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
            top = sorted(genre_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
            return {
                "success": True,
                "total_tracks": total,
                "artists": len(artists or []),
                "albums": len(albums or []),
                "playlists": len(playlists or []),
                "top_genres": [{"genre": g, "count": c} for g, c in top],
            }

        if tool_name == "get_top_played":
            rows = await runner.db.get_most_played(limit=int(args.get("limit") or 10))
            return {"success": True, "tracks": [_slim_track(dict(r)) for r in (rows or [])]}

        if tool_name == "list_artists":
            search = (args.get("search") or "").strip()
            limit = int(args.get("limit") or 25)
            rows = await runner.db.get_all_artists(search_query=search)
            out = [
                {"name": dict(r).get("name"),
                 "track_count": dict(r).get("track_count"),
                 "album_count": dict(r).get("album_count")}
                for r in (rows or [])[:limit]
            ]
            return {"success": True, "count": len(out), "artists": out}

        if tool_name == "get_artist_albums":
            artist = (args.get("artist") or "").strip()
            if not artist:
                return {"success": False, "error": "No artist given."}
            rows = await runner.db.get_albums_by_artist(artist)
            out = [
                {"album": dict(r).get("album"), "year": dict(r).get("year"),
                 "genre": dict(r).get("genre"), "track_count": dict(r).get("track_count")}
                for r in (rows or [])
            ]
            return {"success": True, "artist": artist, "count": len(out), "albums": out}

        if tool_name == "get_album_tracks":
            tracks = await _resolve_album_tracks(runner, (args.get("album") or "").strip(),
                                                 (args.get("artist") or "").strip())
            return {"success": True, "count": len(tracks),
                    "tracks": [_slim_track(t) for t in tracks]}

        if tool_name == "list_playlists":
            rows = await runner.db.get_all_playlists()
            out = [{"name": dict(r).get("name"), "track_count": dict(r).get("track_count")}
                   for r in (rows or [])]
            return {"success": True, "count": len(out), "playlists": out}

        if tool_name == "get_playlist_tracks":
            pid, pname = await _resolve_playlist(runner, (args.get("name") or "").strip())
            if pid is None:
                return {"success": False, "message": f"No playlist matching '{args.get('name')}'."}
            rows = await runner.db.get_tracks_in_playlist(pid)
            return {"success": True, "playlist": pname, "count": len(rows or []),
                    "tracks": [_slim_track(dict(r)) for r in (rows or [])]}

        if tool_name == "get_recently_played":
            rows = await runner.db.get_recent_tracks(limit=int(args.get("limit") or 15))
            return {"success": True, "tracks": [_slim_track(dict(r)) for r in (rows or [])]}

        if tool_name == "get_track_details":
            path = (args.get("path") or "").strip()
            query = (args.get("query") or "").strip()
            if not path and not query:
                path = getattr(runner.engine, "current_path", "") or ""
            if not path and query:
                hit = await runner.db.search_tracks_simple(query, limit=1)
                if hit:
                    path = dict(hit[0]).get("path") or ""
            if not path:
                return {"success": False, "message": "No matching track."}
            full = await runner.db.get_track_full(path)
            if not full:
                return {"success": False, "message": "Track not found."}
            return {"success": True, "track": _slim_track(dict(full))}

        # ── Album / playlist playback (destructive replace → gated when busy) ─
        if tool_name == "play_album":
            album = (args.get("album") or "").strip()
            tracks = await _resolve_album_tracks(runner, album, (args.get("artist") or "").strip())
            if not tracks:
                return {"success": False, "message": f"No album matching '{album}'."}
            return await _play_or_gate(runner, tracks, f"the album '{album}'")

        if tool_name == "play_playlist":
            pid, pname = await _resolve_playlist(runner, (args.get("name") or "").strip())
            if pid is None:
                return {"success": False, "message": f"No playlist matching '{args.get('name')}'."}
            tracks = [dict(r) for r in (await runner.db.get_tracks_in_playlist(pid) or [])]
            if not tracks:
                return {"success": False, "message": f"Playlist '{pname}' is empty."}
            return await _play_or_gate(runner, tracks, f"the playlist '{pname}'")

        return {"success": False, "error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        logger.exception("Agent tool %s failed: %s", tool_name, e)
        return {"success": False, "error": str(e)}


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _resolved_extras(runner: "AssistantRunner", path: str) -> Dict[str, Any]:
    """If the model passed an exact library path, pre-resolve the track dict so
    the handler plays precisely that track instead of re-searching."""
    if not path or runner.db is None:
        return {}
    try:
        track = await runner.db.get_track_full(path)
        if track:
            # Slim first: get_track_full carries a timbre BLOB that must not
            # reach extras/chat-history (bytes aren't JSON-serialisable).
            return {"resolved_track": _slim_track(dict(track))}
    except Exception:
        pass
    return {}


def _play_label(extras: Dict[str, Any], query: str) -> str:
    t = extras.get("resolved_track") if extras else None
    if t:
        return f"'{t.get('title') or t.get('track_title')}' by {t.get('artist') or t.get('artist_name')}"
    return f"'{query}'"


async def _search_qobuz(query: str):
    """Await the thread-looped StreamripSearcher via a future bridged back to
    the app loop. Returns a list of parsed items or an {'error': ...} dict."""
    from utils.streamrip_search import StreamripSearcher

    loop = asyncio.get_running_loop()
    fut: "asyncio.Future" = loop.create_future()

    def _cb(results):
        if not fut.done():
            loop.call_soon_threadsafe(fut.set_result, results)

    StreamripSearcher().search(query, "qobuz", _cb, media_types=["track", "album"], limit=10)
    try:
        return await asyncio.wait_for(fut, timeout=25)
    except asyncio.TimeoutError:
        return {"error": "Online search timed out."}


async def _download_item(item: Dict[str, Any]) -> "Any":
    """Kick off a real Qobuz download for a search result item. Runs only after
    the user has confirmed (armed via agent_request_confirmation)."""
    from utils.assistant_runner import AssistantResponse
    from utils import streamrip_api

    url = item.get("url") or ""
    if not url:
        return AssistantResponse(
            spoken="I couldn't resolve that item's download link, sir.",
            displayed="Download failed: missing item URL.",
            success=False,
        )
    try:
        download_dir = streamrip_api.get_default_download_path()
        await streamrip_api.download(url, download_dir)
        return AssistantResponse(
            spoken=f"Downloading '{item.get('title')}' by {item.get('artist')} now, sir.",
            displayed=f"Started download: **{item.get('title')}** — {item.get('artist')}.",
        )
    except Exception as e:
        logger.exception("Agent download failed: %s", e)
        return AssistantResponse(
            spoken="The download could not be started, sir.",
            displayed=f"Download error: {e}",
            success=False,
        )


async def _resolve_album_tracks(runner: "AssistantRunner", album: str, artist: str) -> List[Dict[str, Any]]:
    """Resolve an album (optionally by artist) to its ordered tracks. Canonicalises
    title+artist casing via album search so the exact-match track query lands."""
    if not album or runner.db is None:
        return []
    albums = await runner.db.get_all_albums(search_query=album)
    al_low, ar_low = album.strip().lower(), artist.strip().lower()
    chosen = None
    for a in (albums or []):
        ad = dict(a)
        if (ad.get("album") or "").strip().lower() == al_low:
            if not ar_low or (ad.get("artist") or "").strip().lower() == ar_low:
                chosen = ad
                break
    if chosen is None and albums:
        chosen = dict(albums[0])  # best fuzzy match
    if chosen is None:
        return []
    rows = await runner.db.get_tracks_by_album(chosen.get("album"), chosen.get("artist"))
    return [dict(r) for r in (rows or [])]


async def _resolve_playlist(runner: "AssistantRunner", name: str):
    """Resolve a playlist name to (id, canonical_name), tolerating fuzzy hits."""
    if not name or runner.db is None:
        return None, None
    rows = await runner.db.get_all_playlists(search_query=name)
    low = name.strip().lower()
    for r in (rows or []):
        rd = dict(r)
        if (rd.get("name") or "").strip().lower() == low:
            return rd.get("id"), rd.get("name")
    if rows:
        rd = dict(rows[0])
        return rd.get("id"), rd.get("name")
    return None, None


async def _play_or_gate(runner: "AssistantRunner", tracks: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """Play an explicit ordered track list now, or ask first if a queue is busy
    (mirrors play_music's non-destructive gating)."""
    if getattr(runner.engine, "queue", None):
        async def _do():
            return runner._stage_tracks(tracks, label)
        return runner.agent_request_confirmation(
            spoken=f"That will replace your current queue with {label}, sir. Shall I proceed?",
            displayed=f"This will **replace your current queue** with {label}. Proceed?",
            on_yes=_do,
        )
    return await runner.agent_play_tracks(tracks, label)
