"""
Assistant action dispatcher.

Takes a parsed Intent (from assistant_intent.py) and runs the corresponding
operation against the existing audio engine + db_manager + streamrip pipeline.
Returns a structured response the UI can render as a bubble and pass to TTS.

The runner is intentionally a thin coordinator. All real work lives in the
modules it delegates to:

  • audio_engine          — playback + queue mutations
  • db_manager            — local library search, track lookup
  • utils.track_graph     — graph traversal for similarity / artist nav
  • streamrip_search /
    streamrip download   — Qobuz search + download (online, optional)

The runner never speaks (TTS) or renders directly; the caller passes the
returned `spoken` text into the TTS layer and the `displayed` text into the
chat UI. Keeping speech out of here makes the runner trivially unit-testable.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Optional, List

from utils import assistant_intent as ai
from utils import track_graph

logger = logging.getLogger(__name__)


@dataclass
class AssistantResponse:
    """Returned to the caller for every dispatched intent."""
    spoken: str
    displayed: str
    success: bool = True
    # Optional structured payload for the UI: tracks resolved, suggestions,
    # error details. Keys are stable across responses so the chat view can
    # render them consistently.
    extras: dict = field(default_factory=dict)
    # Optional high-level action the view should perform after rendering the
    # bubble. Used for long-running work (DSP rebuild) that the runner can't
    # do itself — it needs the AssistantView's banner + progress UI. Stable
    # action names so view dispatch can be a simple switch.
    #   "rebuild_graph": rerun analyser for missing tracks + rebuild edges.
    action: Optional[str] = None
    # When True the handler has populated the queue but deliberately NOT
    # called engine.play(); the view should start playback only after TTS
    # finishes. Used by intents that begin a new playback session so Jarvis
    # finishes his sentence before the music kicks in. Playback-control
    # intents (resume/skip/etc.) act eagerly and leave this False.
    deferred_play: bool = False


@dataclass
class PendingConfirmation:
    """A reusable yes/no routine. Owned by AssistantRunner; cleared when the
    user replies (yes runs `on_yes`, no runs `on_no` if set, anything else
    discards the pending and routes normally as a new intent)."""
    prompt: str
    on_yes_action: Optional[str] = None   # AssistantResponse.action to emit
    on_yes_msg: str = "On it."
    on_no_msg: str = "Understood. Standing by."


# ── Track dict shape used by the audio engine ────────────────────────────────
#
# Keys the engine consumes: path, track_title, artist_name, album_title,
# image_url, duration. Anything else is ignored. We build dicts in this shape
# from the db_manager's row dicts (which use title/artist/album).

def _to_engine_track(row: dict) -> dict:
    """Re-key a db_manager row dict to the shape audio_engine expects."""
    return {
        "path":        row.get("path"),
        "track_title": row.get("title") or row.get("track_title") or "",
        "artist_name": row.get("artist") or row.get("artist_name") or "Unknown Artist",
        "album_title": row.get("album")  or row.get("album_title")  or "Unknown Album",
        "duration":    row.get("duration", 0.0) or 0.0,
        "image_url":   row.get("image_url", "") or "",
    }


def _track_summary(t: dict) -> str:
    title = t.get("track_title") or t.get("title") or "Unknown"
    artist = t.get("artist_name") or t.get("artist") or "Unknown Artist"
    return f"{title} — {artist}"


# ── Main entry point ─────────────────────────────────────────────────────────


class AssistantRunner:
    """Dispatches Intent objects against the live engine + DB.

    The runner is stateful only in that it keeps a 'recent_playback' set so
    `play_similar` walks don't immediately repeat tracks the user just heard.
    Everything else (current track, queue) is read live from audio_engine.
    """

    def __init__(self, db_manager, audio_engine, downloader=None):
        self.db = db_manager
        self.engine = audio_engine
        self.downloader = downloader  # streamrip_api.StreamripAPI or None
        # Cap on the avoid-set so it doesn't grow forever during long sessions.
        self._recent: list[str] = []
        self._recent_cap = 50
        # Pending yes/no confirmation. Set by handlers/the view when they
        # need to ask for permission before doing something expensive (e.g.
        # the DSP analyser sweep). Resolved on the next dispatch when the
        # user replies with INTENT_AFFIRMATIVE / INTENT_NEGATIVE.
        self._pending: Optional[PendingConfirmation] = None

    def queue_confirmation(self, prompt: PendingConfirmation) -> None:
        """Stage a pending yes/no for the next user turn. Replaces any
        previously-pending confirmation; the assistant only ever holds one
        open question at a time."""
        self._pending = prompt

    # ── Jarvis Personality ──────────────────────────────────────────────────

    JARVIS_PHRASES = {
        "affirmative": [
            "Certainly, sir.", "Of course, sir.", "Right away, sir.",
            "As you wish.", "Initiating now.", "Consider it done.",
            "Always a pleasure, sir.", "Very good, sir.", 
            "Systems active. Processing your request.",
            "Execution protocols engaged, sir.",
        ],
        "searching": [
            "Scanning your library database...",
            "Accessing the music graph...",
            "Locating the requested tracks...",
            "Sifting through the archives...",
            "Triangulating metadata signatures...",
            "Database query in progress, sir.",
        ],
        "error": [
            "I'm afraid I've encountered a system error, sir.",
            "My apologies, but that action was unsuccessful.",
            "It seems the system is unresponsive to that request.",
            "I've hit a bit of a snag in the audio sub-system, sir.",
            "Logic circuits seem to be reporting a conflict, sir.",
        ],
        "not_found": [
            "I'm afraid I couldn't find a match for '{query}', sir.",
            "My apologies, but '{query}' is not in your local library.",
            "It seems '{query}' is missing from the database.",
            "Search protocols completed, but '{query}' remains elusive.",
            "I've searched every corner of the drive, but '{query}' isn't here.",
        ],
        "unknown": [
            "I'm afraid I don't understand that command, sir.",
            "My apologies, sir, but that is not in my protocols.",
            "Could you rephrase that? I didn't quite catch the intent.",
            "I'm sorry, sir. My training does not cover that specific phrasing.",
            "I'm having trouble parsing that request, sir.",
        ],
        "playback_control": [
            "Of course, sir. {action}.",
            "As you wish. {action}.",
            "Understood. {action}.",
            "Right away. {action}.",
            "Adjusting the output stream. {action}.",
        ],
        "discovery": [
            "Initiating similarity sequence, sir.",
            "Accessing the acoustic graph. One moment...",
            "Expanding the playback horizon, sir.",
            "Cross-referencing acoustic signatures...",
            "Heuristics suggest this might suit your mood, sir.",
            "Analyzing the sonic landscape. One moment...",
        ],
        "status": [
            "Currently processing, sir.",
            "Systems are green. The track is {track}.",
            "This is {track} by {artist}, sir.",
            "Telemetry reports we are listening to {track}.",
        ],
        "greeting": [
            "At your service, sir. I've mapped your library — what can I do for you?",
            "Systems online. Library graph fully indexed. How may I assist?",
            "Ready for your commands, sir. The music network is at your disposal.",
            "Good to see you, sir. I'm ready to manage your collection.",
        ]
    }

    def _say(self, category: str, **kwargs) -> str:
        """Pick a random phrase from a category and format it with kwargs."""
        phrases = self.JARVIS_PHRASES.get(category, ["Yes?"])
        return random.choice(phrases).format(**kwargs)

    # ── Public dispatch ─────────────────────────────────────────────────────

    async def dispatch(self, intent: ai.Intent) -> AssistantResponse:
        """Route an Intent to its handler. Catches and reports handler errors
        so the chat UI always gets a renderable response.

        If a confirmation is pending, INTENT_AFFIRMATIVE resolves it (running
        the queued action), INTENT_NEGATIVE cancels it, and anything else
        clears the pending and routes normally as a new request — the user
        moved on, treat their input as a fresh intent rather than ambiguously
        re-asking."""
        # Resolve pending confirmation first.
        pending = self._pending
        if pending is not None:
            if intent.name == ai.INTENT_AFFIRMATIVE:
                self._pending = None
                return AssistantResponse(
                    spoken=pending.on_yes_msg,
                    displayed=pending.on_yes_msg,
                    action=pending.on_yes_action,
                )
            if intent.name == ai.INTENT_NEGATIVE:
                self._pending = None
                return AssistantResponse(
                    spoken=pending.on_no_msg,
                    displayed=pending.on_no_msg,
                )
            # Anything else: drop pending, fall through to normal dispatch.
            self._pending = None

        try:
            handler = self._INTENT_DISPATCH.get(intent.name, AssistantRunner._handle_unknown)
            return await handler(self, intent)
        except Exception as exc:
            logger.exception("AssistantRunner: handler failed for %s", intent.name)
            return AssistantResponse(
                spoken=self._say("error"),
                displayed=f"Error: {exc}",
                success=False,
            )

    async def dispatch_text(self, text: str) -> AssistantResponse:
        """Convenience: parse + dispatch in one call."""
        intent = ai.parse(text)
        return await self.dispatch(intent)

    # ── Recent-playback tracking ────────────────────────────────────────────

    def _remember(self, path: str) -> None:
        if not path:
            return
        if path in self._recent:
            self._recent.remove(path)
        self._recent.append(path)
        if len(self._recent) > self._recent_cap:
            self._recent.pop(0)

    def _avoid_set(self) -> set[str]:
        return set(self._recent)

    # ── Resolution helpers ──────────────────────────────────────────────────

    async def _resolve_query(self, query: str) -> Optional[dict]:
        """Find the single best local-library match for a free-text query."""
        if not query:
            return None
        hits = await self._resolve_queries(query, limit=1)
        return hits[0] if hits else None

    async def _resolve_queries(self, query: str, limit: int = 25) -> list[dict]:
        """Like _resolve_query but returns up to limit matches with fuzzy matching fallback."""
        if not query:
            return []
            
        # 1. Try simple database search first
        hits = await self.db.search_tracks_simple(query, limit=limit)
        if hits:
            for h in hits:
                h["fuzzy_match"] = False
            return hits
            
        # 2. Fuzzy string matching fallback across the whole library
        all_tracks = await self.db.get_all_tracks()
        if not all_tracks:
            return []
            
        import difflib
        scored_tracks = []
        q_lower = query.lower()
        for track in all_tracks:
            title = (track.get("title") or "").lower()
            artist = (track.get("artist") or "").lower()
            combined = f"{title} {artist}"
            
            r_title = difflib.SequenceMatcher(None, q_lower, title).ratio()
            r_artist = difflib.SequenceMatcher(None, q_lower, artist).ratio()
            r_comb = difflib.SequenceMatcher(None, q_lower, combined).ratio()
            
            score = max(r_title, r_artist, r_comb)
            if score > 0.65:
                scored_tracks.append((score, track))
                
        if not scored_tracks:
            return []
            
        scored_tracks.sort(key=lambda x: x[0], reverse=True)
        results = []
        for _, t in scored_tracks[:limit]:
            results.append({
                "path": t.get("path"),
                "title": t.get("title") or t.get("track_title") or "",
                "artist": t.get("artist") or t.get("artist_name") or "Unknown Artist",
                "album": t.get("album") or t.get("album_title") or "Unknown Album",
                "duration": t.get("duration", 0.0) or 0.0,
                "image_url": t.get("image_url") or "",
                "fuzzy_match": True,
            })
        return results

    # ── Handlers ────────────────────────────────────────────────────────────

    async def _handle_play_now(self, intent: ai.Intent) -> AssistantResponse:
        # Fetch a broad set of candidates. A specific title query usually
        # returns 1; an artist query like 'radiohead' returns many. We treat
        # the two cases differently: single match → play it, multiple →
        # enqueue the whole set with a randomised starting point so the user
        # gets variety without us toggling their shuffle setting.
        hits = await self._resolve_queries(intent.query or "", limit=25)
        if not hits:
            return AssistantResponse(
                spoken=f"I couldn't find anything matching '{intent.query}', sir.",
                displayed=f"No local match for **{intent.query}**.",
                success=False,
            )

        engine_tracks = [_to_engine_track(t) for t in hits]
        first = hits[0]
        is_fuzzy = first.get("fuzzy_match", False)
        prefix_spoken = f"I couldn't find exactly '{intent.query}', sir. Playing " if is_fuzzy else ""
        prefix_displayed = f"No exact match for '{intent.query}'. Playing " if is_fuzzy else ""

        if len(engine_tracks) == 1:
            self.engine.set_queue(engine_tracks, start_index=0)
            self._remember(engine_tracks[0]["path"])
            return AssistantResponse(
                spoken=f"{self._say('affirmative')} {prefix_spoken}{first.get('title')} by {first.get('artist')}.",
                displayed=f"Certainly. {prefix_displayed}**{first.get('title')}** — {first.get('artist')}",
                extras={"track": first},
                deferred_play=True,
            )

        # Multi-match: start from a random index so repeated 'play radiohead'
        # doesn't open with the same track every time.
        start = random.randint(0, len(engine_tracks) - 1)
        self.engine.set_queue(engine_tracks, start_index=start)
        self._remember(engine_tracks[start]["path"])
        first = hits[start]
        return AssistantResponse(
            spoken=(
                f"{self._say('affirmative')} {prefix_spoken}{len(engine_tracks)} tracks matching "
                f"'{intent.query}'. Starting with {first.get('title')} by {first.get('artist')}."
            ),
            displayed=(
                f"Queued **{len(engine_tracks)}** matches for **{intent.query}**. "
                f"{prefix_displayed}**{first.get('title')}** — {first.get('artist')}."
            ),
            extras={"queued": len(engine_tracks), "first": first},
            deferred_play=True,
        )

    async def _handle_play_mood(self, intent: ai.Intent) -> AssistantResponse:
        mood = (intent.query or "").lower().strip()
        if not mood:
            return AssistantResponse(
                spoken="Which mood, sir?",
                displayed="I need a mood — try 'play something chill' or 'play upbeat music'.",
                success=False,
            )

        from utils import track_graph as tg
        if mood not in tg.MOOD_PROFILES:
            return AssistantResponse(
                spoken=f"I don't have a profile for '{mood}', sir.",
                displayed=(
                    f"Unknown mood **{mood}**. Try one of: "
                    f"{', '.join(sorted(tg.MOOD_PROFILES.keys()))}."
                ),
                success=False,
            )

        tracks = await tg.tracks_by_mood(self.db, mood, limit=12)
        if not tracks:
            return AssistantResponse(
                spoken=(
                    "I haven't analysed enough of your library to pick by mood yet. "
                    "Let the indexer finish, then try again."
                ),
                displayed=(
                    "Mood search needs DSP features. Wait for the analyser to "
                    "finish (banner at the top of this sheet), then ask again."
                ),
                success=False,
            )

        engine_tracks = [_to_engine_track(t) for t in tracks]
        self.engine.set_queue(engine_tracks, start_index=0)
        for t in engine_tracks:
            self._remember(t["path"])

        first = tracks[0]
        return AssistantResponse(
            spoken=(
                f"{self._say('discovery')} Queued {len(tracks)} {mood} tracks. "
                f"Opening with {first.get('title')} by {first.get('artist')}."
            ),
            displayed=(
                f"Queued **{len(tracks)}** {mood} tracks based on DSP profile. "
                f"Starting with **{first.get('title')}** — {first.get('artist')}."
            ),
            extras={"mood": mood, "queued": len(tracks)},
            deferred_play=True,
        )

    async def _handle_play_random(self, _intent: ai.Intent) -> AssistantResponse:
        tracks = await self.db.get_all_tracks()
        if not tracks:
            return AssistantResponse(
                spoken="Your library is currently empty, sir. Please configure your music folder first.",
                displayed="Library is empty — cannot play a random track.",
                success=False,
            )

        engine_tracks = [_to_engine_track(t) for t in tracks]
        random.shuffle(engine_tracks)

        self.engine.is_shuffle = True
        self.engine.set_queue(engine_tracks, start_index=0)
        self._remember(engine_tracks[0]["path"])

        first = engine_tracks[0]
        title = first.get("track_title") or "Unknown Track"
        artist = first.get("artist_name") or "Unknown Artist"
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Initiating shuffle play. Starting with {title} by {artist}.",
            displayed=f"Shuffle play active. Queued **{len(engine_tracks)}** tracks. Starting with: **{title}** — {artist}",
            extras={"track": first, "queued": len(engine_tracks)},
            deferred_play=True,
        )

    async def _handle_queue_add(self, intent: ai.Intent) -> AssistantResponse:
        track = await self._resolve_query(intent.query or "")
        if track is None:
            return AssistantResponse(
                spoken=f"I couldn't find a track matching '{intent.query}'.",
                displayed=f"No local match for **{intent.query}**.",
                success=False,
            )
        engine_track = _to_engine_track(track)
        if not self.engine.queue:
            self.engine.set_queue([engine_track], start_index=0)
            verb = "Playing"
        else:
            self.engine.queue_last(engine_track)
            verb = "Added to queue"
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} {verb}: {track.get('title')} by {track.get('artist')}.",
            displayed=f"{verb}: **{track.get('title')}** — {track.get('artist')}",
            extras={"track": track},
        )

    async def _handle_queue_next(self, intent: ai.Intent) -> AssistantResponse:
        track = await self._resolve_query(intent.query or "")
        if track is None:
            return AssistantResponse(
                spoken=f"I couldn't find a track matching '{intent.query}'.",
                displayed=f"No local match for **{intent.query}**.",
                success=False,
            )
        engine_track = _to_engine_track(track)
        if not self.engine.queue:
            self.engine.set_queue([engine_track], start_index=0)
            verb = "Playing"
        else:
            self.engine.queue_next(engine_track)
            verb = "Playing next"
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} {verb}: {track.get('title')} by {track.get('artist')}.",
            displayed=f"{verb}: **{track.get('title')}** — {track.get('artist')}",
            extras={"track": track},
        )

    async def _handle_play_similar(self, _intent: ai.Intent) -> AssistantResponse:
        seed_path = self.engine.current_path
        if not seed_path:
            return AssistantResponse(
                spoken="Nothing is playing right now.",
                displayed="No current track — start something first, then ask for similar tracks.",
                success=False,
            )

        # Prefer acoustic neighbours; fall back to artist neighbours when the
        # current track lacks DSP features (the analyser hasn't reached it yet).
        nbrs = await track_graph.neighbors(self.db, seed_path, k=10, edge_kind=track_graph.KIND_ACOUSTIC)
        kind_used = "acoustic"
        if not nbrs:
            nbrs = await track_graph.neighbors(self.db, seed_path, k=10, edge_kind=track_graph.KIND_ARTIST)
            kind_used = "artist (no DSP features yet for this track)"
        if not nbrs:
            return AssistantResponse(
                spoken="I don't have enough information to find similar tracks yet.",
                displayed=(
                    "The music graph hasn't been built for this track yet. "
                    "Open Settings → Permissions to enable file access, then "
                    "re-run the assistant initialisation."
                ),
                success=False,
            )

        avoid = self._avoid_set()
        avoid.add(seed_path)
        chosen = [n for n in nbrs if n["path"] not in avoid] or nbrs
        # Drop a short walk into the queue: first track plays next, the rest
        # are appended for autoplay continuity.
        walk_paths = [chosen[0]["path"]]
        try:
            extra = await track_graph.walk(
                self.db, chosen[0]["path"],
                length=4, edge_kind=track_graph.KIND_ACOUSTIC,
                avoid=avoid,
            )
            walk_paths.extend(extra)
        except Exception as exc:
            logger.warning("track_graph.walk failed: %s", exc)

        added = 0
        for p in walk_paths:
            row = await self.db.get_track_full(p)
            if not row:
                continue
            self.engine.queue_last(_to_engine_track(row))
            self._remember(p)
            added += 1

        first_row = await self.db.get_track_full(walk_paths[0]) if walk_paths else None
        first_name = (
            f"{first_row.get('title')} — {first_row.get('artist')}"
            if first_row else "a similar track"
        )
        return AssistantResponse(
            spoken=f"{self._say('discovery')} I've queued {added} tracks similar to this. Starting with {first_name}.",
            displayed=(
                f"Similarity sequence initiated. Queued **{added}** tracks "
                f"(via {kind_used}). Next: **{first_name}**."
            ),
            extras={"added": added, "kind": kind_used},
        )

    async def _handle_play_more_by(self, _intent: ai.Intent) -> AssistantResponse:
        seed_path = self.engine.current_path
        if not seed_path:
            return AssistantResponse(
                spoken="Nothing is playing right now.",
                displayed="No current track — pick a song first.",
                success=False,
            )
        nbrs = await track_graph.neighbors(self.db, seed_path, k=10, edge_kind=track_graph.KIND_ARTIST)
        if not nbrs:
            return AssistantResponse(
                spoken="I don't have other tracks by this artist in your library.",
                displayed="No other tracks by this artist were found locally.",
                success=False,
            )
        avoid = self._avoid_set()
        added = 0
        for n in nbrs:
            if n["path"] in avoid:
                continue
            row = await self.db.get_track_full(n["path"])
            if not row:
                continue
            self.engine.queue_last(_to_engine_track(row))
            self._remember(n["path"])
            added += 1
            if added >= 5:
                break
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Queued {added} more by this artist.",
            displayed=f"Queued **{added}** more tracks by {self.engine.current_artist or 'this artist'}.",
            extras={"added": added},
        )

    async def _handle_download(self, intent: ai.Intent) -> AssistantResponse:
        if not self.downloader:
            return AssistantResponse(
                spoken="Downloads aren't wired up in the assistant yet.",
                displayed="Download intent recognised, but no downloader is bound. Use Search → Download for now.",
                success=False,
            )
        # Defer to the existing streamrip pipeline. The runner only forms the
        # request; the downloader handles auth, quality selection, and IO.
        try:
            await self.downloader.download_query(intent.query or "")
        except Exception as exc:
            return AssistantResponse(
                spoken="Couldn't start that download.",
                displayed=f"Download failed: {exc}",
                success=False,
            )
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Started downloading {intent.query}.",
            displayed=f"Started download: **{intent.query}**",
        )

    async def _handle_skip(self, _intent: ai.Intent) -> AssistantResponse:
        if not self.engine.queue:
            return AssistantResponse(
                spoken="The queue is empty.",
                displayed="Nothing to skip — queue is empty.",
                success=False,
            )
        self.engine.next()
        return AssistantResponse(
            spoken=self._say("playback_control", action="Skipping"),
            displayed="Skipped."
        )

    async def _handle_prev(self, _intent: ai.Intent) -> AssistantResponse:
        if not self.engine.queue:
            return AssistantResponse(
                spoken="The queue is empty.",
                displayed="Nothing to go back to — queue is empty.",
                success=False,
            )
        self.engine.previous()
        return AssistantResponse(
            spoken=self._say("playback_control", action="Going back"),
            displayed="Previous track."
        )

    async def _handle_pause(self, _intent: ai.Intent) -> AssistantResponse:
        if not self.engine.is_playing:
            return AssistantResponse(spoken="Already paused.", displayed="Already paused.")
        self.engine.pause()
        return AssistantResponse(
            spoken=self._say("playback_control", action="Pausing playback"),
            displayed="Paused."
        )

    async def _handle_resume(self, _intent: ai.Intent) -> AssistantResponse:
        if self.engine.is_playing:
            return AssistantResponse(spoken="Already playing.", displayed="Already playing.")
        self.engine.play()
        return AssistantResponse(
            spoken=self._say("playback_control", action="Resuming playback"),
            displayed="Resuming playback."
        )

    async def _handle_stop(self, _intent: ai.Intent) -> AssistantResponse:
        self.engine.stop()
        return AssistantResponse(
            spoken=self._say("playback_control", action="Stopping the music"),
            displayed="Stopped."
        )

    async def _handle_clear_queue(self, _intent: ai.Intent) -> AssistantResponse:
        self.engine.stop()
        self.engine.queue = []
        try:
            self.engine.dispatch("on_queue_mutated")
        except Exception:
            pass
        return AssistantResponse(
            spoken=self._say("playback_control", action="Queue cleared"),
            displayed="Queue cleared."
        )

    async def _handle_shuffle(self, _intent: ai.Intent) -> AssistantResponse:
        # Engine maintains an `is_shuffle` flag; flipping it covers the
        # 'toggle shuffle' phrasings and also re-randomises the next pick.
        prev = getattr(self.engine, "is_shuffle", False)
        new = not prev
        setattr(self.engine, "is_shuffle", new)
        verb = "on" if new else "off"
        return AssistantResponse(
            spoken=self._say("playback_control", action=f"Shuffle {verb}"),
            displayed=f"Shuffle {verb}."
        )

    async def _handle_mute(self, _intent: ai.Intent) -> AssistantResponse:
        # We simulate mute by pausing for now as the engine lacks a 
        # direct volume-0 hook in the current bridge version.
        self.engine.pause()
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Silencing the output.",
            displayed="Audio muted (paused), sir."
        )

    async def _handle_unmute(self, _intent: ai.Intent) -> AssistantResponse:
        self.engine.play()
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Restoring audio output.",
            displayed="Audio unmuted (resumed), sir."
        )

    async def _handle_now_playing(self, _intent: ai.Intent) -> AssistantResponse:
        title = getattr(self.engine, "current_track", "") or ""
        artist = getattr(self.engine, "current_artist", "") or ""
        if not title:
            return AssistantResponse(spoken="Nothing is playing.", displayed="No current track.")
        return AssistantResponse(
            spoken=self._say("status", track=title, artist=artist),
            displayed=f"**{title}** — {artist}",
        )

    async def _handle_rescan_dsp(self, _intent: ai.Intent) -> AssistantResponse:
        """Manual trigger for 'rescan/reindex/analyse my library'. Always
        emits the rebuild_graph action — the view decides whether the work
        is needed (analyser has nothing to do when all tracks are already
        analysed) and shows the banner accordingly."""
        from utils import track_graph as tg
        try:
            missing = await self.db.get_tracks_missing_features(tg.FEATURES_VERSION)
        except Exception as exc:
            logger.warning("rescan_dsp missing-check failed: %s", exc)
            missing = []
        count = len(missing)
        if count == 0:
            return AssistantResponse(
                spoken="Library already fully analysed, sir.",
                displayed="Every track has DSP features — nothing to scan.",
            )
        return AssistantResponse(
            spoken=f"Acknowledged. Analysing {count} tracks now.",
            displayed=f"Running DSP analysis on **{count}** tracks…",
            action="rebuild_graph",
        )

    async def _handle_help(self, _intent: ai.Intent) -> AssistantResponse:
        spoken_msg = (
            "I can manage your playback, queue tracks, navigate by mood, create playlists, "
            "or walk the acoustic similarity graph, sir. Just say 'play some chill music' "
            "or 'more by this artist' to begin."
        )
        displayed_msg = (
            "### Jarvis System Capabilities\n\n"
            "*   **Playback**: `play [song/artist]`, `pause`, `resume`, `skip`, `prev`, `shuffle`\n"
            "*   **Acoustic Moods**: `play chill`, `play upbeat`, `play energetic`\n"
            "*   **Similarity Graph**: `play similar`, `more like this`, `more by this artist`\n"
            "*   **Playlists**: `create playlist [name]`, "
            "`create [mood] playlist called [name]` (library-wide DSP-ranked, "
            "e.g. *create a chill playlist called Late Night*), "
            "`add this to [playlist]`\n"
            "*   **Sub-systems**: `rescan dsp`, `clear queue`, `download [song]`"
        )
        return AssistantResponse(spoken=spoken_msg, displayed=displayed_msg)

    async def _handle_playlist_create(self, intent: ai.Intent) -> AssistantResponse:
        name = (intent.query or "").strip()
        if not name:
            return AssistantResponse(
                spoken="What should I name the playlist, sir?",
                displayed="Please specify a playlist name.",
                success=False,
            )
        try:
            await self.db.create_playlist(name)
            return AssistantResponse(
                spoken=f"{self._say('affirmative')} I have created the playlist '{name}' for you.",
                displayed=f"Created playlist: **{name}**",
            )
        except Exception:
            return AssistantResponse(
                spoken=f"It seems a playlist called '{name}' already exists, sir.",
                displayed=f"Playlist **{name}** already exists.",
                success=False,
            )

    async def _handle_playlist_auto(self, intent: ai.Intent) -> AssistantResponse:
        """Mood-driven playlist: create the playlist row, then populate it
        with the top library-wide matches for the requested mood profile.
        Uses the same DSP feature store the assistant's library sweep
        writes into, so any track Jarvis has already analysed is fair
        game without re-running DSP. Tracks without features are skipped
        — the user is told to run a rescan if too few are available."""
        from utils.auto_playlist import generate_mood_playlist
        from utils import track_graph as tg

        name = (intent.query or "").strip()
        mood = (intent.extras.get("mood") or "").strip().lower()

        if not name:
            return AssistantResponse(
                spoken="What should I name the playlist, sir?",
                displayed="Please specify a playlist name.",
                success=False,
            )
        if not mood or mood not in tg.MOOD_PROFILES:
            return AssistantResponse(
                spoken=(
                    "Which mood should the playlist be, sir? Try chill, "
                    "energetic, dark, bright, fast, slow, and so on."
                ),
                displayed=(
                    "Tell me the mood too — e.g. *create a chill playlist "
                    "called Late Night*. Known moods: "
                    f"{', '.join(sorted(tg.MOOD_PROFILES.keys()))}."
                ),
                success=False,
            )

        tracks = await generate_mood_playlist(self.db, mood, target_length=20)
        if not tracks:
            return AssistantResponse(
                spoken=(
                    "I don't have enough analysed tracks yet. Ask me to "
                    "rescan the library first, sir."
                ),
                displayed=(
                    "Mood-driven playlist needs DSP-analysed tracks. Ask me "
                    "to **rescan** the library first."
                ),
                success=False,
            )

        try:
            playlist_id = await self.db.create_playlist(name)
        except Exception:
            return AssistantResponse(
                spoken=f"It seems a playlist called '{name}' already exists, sir.",
                displayed=f"Playlist **{name}** already exists.",
                success=False,
            )

        for t in tracks:
            try:
                await self.db.add_track_to_playlist(playlist_id, t["path"])
            except Exception as ex:
                logger.warning(
                    "playlist_auto: add_track failed for %s: %s", t["path"], ex
                )

        first = tracks[0]
        return AssistantResponse(
            spoken=(
                f"{self._say('affirmative')} I've built '{name}' with "
                f"{len(tracks)} {mood} tracks, opening with "
                f"{first.get('title') or 'the top match'}."
            ),
            displayed=(
                f"Created **{name}** with **{len(tracks)}** {mood} tracks "
                f"ranked over the library's DSP features. Opening with "
                f"**{first.get('title') or first['path']}**."
            ),
            extras={"playlist_id": playlist_id, "queued": len(tracks), "mood": mood},
        )

    async def _handle_playlist_add(self, intent: ai.Intent) -> AssistantResponse:
        playlist_name = intent.extras.get("playlist")
        track_query = intent.extras.get("track")
        
        if not playlist_name:
            return AssistantResponse(
                spoken="Which playlist should I add it to, sir?",
                displayed="Please specify a playlist name.",
                success=False,
            )
            
        # Find playlist
        playlists = await self.db.get_all_playlists()
        target_playlist = None
        if playlists:
            for p in playlists:
                if p["name"].lower() == playlist_name.lower():
                    target_playlist = p
                    break
            if not target_playlist:
                import difflib
                best_p = None
                best_score = 0.0
                for p in playlists:
                    score = difflib.SequenceMatcher(None, playlist_name.lower(), p["name"].lower()).ratio()
                    if score > best_score:
                        best_score = score
                        best_p = p
                if best_score > 0.6:
                    target_playlist = best_p
                    
        if not target_playlist:
            return AssistantResponse(
                spoken=f"I couldn't find a playlist named '{playlist_name}', sir.",
                displayed=f"Playlist **{playlist_name}** not found.",
                success=False,
            )
            
        track_path = None
        track_title = ""
        track_artist = ""
        
        if track_query:
            track = await self._resolve_query(track_query)
            if not track:
                return AssistantResponse(
                    spoken=f"I couldn't find a track matching '{track_query}', sir.",
                    displayed=f"No local match for **{track_query}**.",
                    success=False,
                )
            track_path = track["path"]
            track_title = track["title"]
            track_artist = track["artist"]
        else:
            # Add currently playing track
            track_path = self.engine.current_path
            if not track_path:
                return AssistantResponse(
                    spoken="Nothing is playing right now, sir.",
                    displayed="No current track to add.",
                    success=False,
                )
            track_title = getattr(self.engine, "current_track", "") or "Unknown Song"
            track_artist = getattr(self.engine, "current_artist", "") or "Unknown Artist"
            
        await self.db.add_track_to_playlist(target_playlist["id"], track_path)
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Added '{track_title}' by {track_artist} to your '{target_playlist['name']}' playlist.",
            displayed=f"Added to **{target_playlist['name']}**: **{track_title}** — {track_artist}",
        )

    async def _handle_playlist_play(self, intent: ai.Intent) -> AssistantResponse:
        playlist_name = (intent.query or "").strip()
        if not playlist_name:
            return AssistantResponse(
                spoken="Which playlist would you like to play, sir?",
                displayed="Please specify a playlist name.",
                success=False,
            )
            
        # Find playlist
        playlists = await self.db.get_all_playlists()
        target_playlist = None
        if playlists:
            for p in playlists:
                if p["name"].lower() == playlist_name.lower():
                    target_playlist = p
                    break
            if not target_playlist:
                import difflib
                best_p = None
                best_score = 0.0
                for p in playlists:
                    score = difflib.SequenceMatcher(None, playlist_name.lower(), p["name"].lower()).ratio()
                    if score > best_score:
                        best_score = score
                        best_p = p
                if best_score > 0.6:
                    target_playlist = best_p
                    
        if not target_playlist:
            return AssistantResponse(
                spoken=f"I couldn't find a playlist named '{playlist_name}', sir.",
                displayed=f"Playlist **{playlist_name}** not found.",
                success=False,
            )
            
        tracks = await self.db.get_tracks_in_playlist(target_playlist["id"])
        if not tracks:
            return AssistantResponse(
                spoken=f"The playlist '{target_playlist['name']}' is empty, sir.",
                displayed=f"Playlist **{target_playlist['name']}** is empty.",
                success=False,
            )
            
        engine_tracks = [_to_engine_track(t) for t in tracks]
        self.engine.set_queue(engine_tracks, start_index=0)

        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Playing playlist '{target_playlist['name']}'.",
            displayed=f"Now playing playlist: **{target_playlist['name']}** ({len(engine_tracks)} tracks)",
            deferred_play=True,
        )

    async def _handle_unknown(self, intent: ai.Intent) -> AssistantResponse:
        # Last-resort fallback: treat the whole utterance as a library search.
        text = (intent.raw or "").strip()
        if not text:
            return AssistantResponse(
                spoken="I didn't catch that.",
                displayed="(empty input)",
                success=False,
            )
        track = await self._resolve_query(text)
        if track is None:
            return AssistantResponse(
                spoken=self._say("unknown"),
                displayed=f"I didn't understand that. Try 'help' to see my capabilities.",
                success=False,
            )
        engine_track = _to_engine_track(track)
        self.engine.set_queue([engine_track], start_index=0)
        self._remember(engine_track["path"])

        is_fuzzy = track.get("fuzzy_match", False)
        prefix_spoken = f"I couldn't find exactly '{text}', sir. Playing " if is_fuzzy else "Playing "
        prefix_displayed = f"No exact match. Best guess: playing " if is_fuzzy else "Best guess: playing "

        return AssistantResponse(
            spoken=f"{prefix_spoken}{track.get('title')} by {track.get('artist')}.",
            displayed=f"{prefix_displayed}**{track.get('title')}** — {track.get('artist')}",
            extras={"track": track, "fallback": True},
            deferred_play=True,
        )

    # ── Dispatch table ──────────────────────────────────────────────────────
    # Filled below the class so the method references resolve.


AssistantRunner._INTENT_DISPATCH = {
    ai.INTENT_PLAY_NOW:      AssistantRunner._handle_play_now,
    ai.INTENT_QUEUE_ADD:     AssistantRunner._handle_queue_add,
    ai.INTENT_QUEUE_NEXT:    AssistantRunner._handle_queue_next,
    ai.INTENT_PLAY_SIMILAR:  AssistantRunner._handle_play_similar,
    ai.INTENT_PLAY_MORE_BY:  AssistantRunner._handle_play_more_by,
    ai.INTENT_PLAY_MOOD:     AssistantRunner._handle_play_mood,
    ai.INTENT_PLAY_RANDOM:    AssistantRunner._handle_play_random,
    ai.INTENT_DOWNLOAD:      AssistantRunner._handle_download,
    ai.INTENT_SKIP:          AssistantRunner._handle_skip,
    ai.INTENT_PREV:          AssistantRunner._handle_prev,
    ai.INTENT_PAUSE:         AssistantRunner._handle_pause,
    ai.INTENT_RESUME:        AssistantRunner._handle_resume,
    ai.INTENT_STOP:          AssistantRunner._handle_stop,
    ai.INTENT_CLEAR_QUEUE:   AssistantRunner._handle_clear_queue,
    ai.INTENT_SHUFFLE:       AssistantRunner._handle_shuffle,
    ai.INTENT_MUTE:          AssistantRunner._handle_mute,
    ai.INTENT_UNMUTE:        AssistantRunner._handle_unmute,
    ai.INTENT_NOW_PLAYING:   AssistantRunner._handle_now_playing,
    ai.INTENT_RESCAN_DSP:    AssistantRunner._handle_rescan_dsp,
    ai.INTENT_PLAYLIST_CREATE: AssistantRunner._handle_playlist_create,
    ai.INTENT_PLAYLIST_AUTO:   AssistantRunner._handle_playlist_auto,
    ai.INTENT_PLAYLIST_ADD:    AssistantRunner._handle_playlist_add,
    ai.INTENT_PLAYLIST_PLAY:   AssistantRunner._handle_playlist_play,
    ai.INTENT_HELP:          AssistantRunner._handle_help,
    ai.INTENT_UNKNOWN:       AssistantRunner._handle_unknown,
}
