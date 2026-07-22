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

import copy
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Optional, List, Callable

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
    intent: Optional[ai.Intent] = None


@dataclass
class PendingConfirmation:
    """A reusable yes/no routine. Owned by AssistantRunner; cleared when the
    user replies (yes runs `on_yes`, no runs `on_no` if set, anything else
    discards the pending and routes normally as a new intent)."""
    prompt: str
    on_yes_action: Optional[str] = None   # AssistantResponse.action to emit
    on_yes_msg: str = "On it."
    on_no_msg: str = "Understood. Standing by."
    on_yes_callback: Optional[Callable] = None
    on_no_callback: Optional[Callable] = None


@dataclass
class ChoiceOption:
    id: str
    title: str
    subtitle: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class PendingChoice:
    prompt: str
    options: list[ChoiceOption]
    on_select_callback: Optional[Callable[[ChoiceOption], Any]] = None




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
        self._pending_choice: Optional[PendingChoice] = None
        self._history_cache: Optional[dict] = None
        # Optional injected callable returning the live in-memory history
        # list. When set (by AssistantView), the resolver skips the disk
        # round-trip through ChatMemoryManager and reads in-process state
        # directly. Reset per-dispatch by dispatch() / dispatch_text().
        self._history_provider: Optional[Callable[[], list]] = None
        # ── AI-agent (LLM tool-calling) loop state ──────────────────────────
        # Reset per agent turn by agent_reset(). Tools mutate these via the
        # agent_* helpers so all runner state stays owned by the runner.
        self._agent_deferred_play: bool = False
        self._agent_interrupt: Optional["AssistantResponse"] = None
        self._agent_last_extras: dict = {}
        self._agent_tools_used: list[str] = []
        # Parsed AssistantConfig cache, invalidated by config-file mtime so we
        # don't re-read + re-serialise the TOML on every single agent turn.
        self._cfg_cache = None
        self._cfg_mtime: float = 0.0

    def queue_confirmation(self, prompt: PendingConfirmation) -> None:
        """Stage a pending yes/no for the next user turn. Replaces any
        previously-pending confirmation; the assistant only ever holds one
        open question at a time."""
        self._pending = prompt

    def queue_choice(self, choice: PendingChoice) -> None:
        """Stage a multi-option choice for the next user turn."""
        self._pending_choice = choice

    # ── Jarvis Personality ──────────────────────────────────────────────────

    JARVIS_PHRASES = {
        "affirmative": [
            "Certainly, sir.", 
            "Of course, sir.", 
            "Right away, sir.",
            "As you wish.", 
            "Initiating now.", 
            "Consider it done, sir.",
            "Always a pleasure, sir.", 
            "Very good, sir.", 
            "Systems active. Processing your request.",
            "Execution protocols engaged, sir.",
            "Understood, sir. Initiating sequence.",
            "Configuring parameters now. One moment.",
            "Compliance confirmed. Proceeding, sir.",
            "Understood. Executing command.",
            "Task initialized. I am on it, sir.",
            "Command logged. Processing protocols.",
            "Acknowledged, sir. Routing instruction.",
            "Right away. Adjusting operational parameters.",
            "Confirming instruction. Executing now.",
            "Systems aligned, sir. Commencing operation.",
            "Indeed, sir. Commencing requested sequence.",
            "Very good, sir. Dispatched to the main controller.",
            "Executing with precision, sir.",
            "Your command is my directive, sir.",
            "Making adjustments. One moment.",
            "Undertaking the task, sir.",
            "Signal routing updated. Proceeding.",
            "Initiating procedures, sir.",
            "Initiating your request, sir.",
            "Command register locked. Executing, sir.",
            "Processing your command, sir.",
            "Your instruction is prioritized and dispatched, sir.",
            "Task registered. Beginning background execution.",
            "Affirmative, sir. Aligning system states.",
            "Routing requests to the controllers, sir.",
            "Initiating stream configuration, sir.",
            "At once, sir. Executing command.",
            "Adjusting music engine registers for you, sir.",
            "Acknowledged. Setting state transitions.",
            "Command verified. Commencing execution, sir.",
            "Right away, sir. Updating registries.",
            "Initiating the procedure, sir.",
            "Your directive is registered. Executing.",
            "Directing the audio subsystem to execute, sir.",
            "Understood. Applying adjustments to active deck.",
            "Command accepted, sir.",
            "Confirmed. Applying high-performance directives.",
            "Absolutely, sir. Commencing command pattern.",
            "Aligning database and playback controllers.",
            "Sequence initiated. Ready for next input, sir."
        ],
        "searching": [
            "Scanning library database...",
            "Accessing the music graph...",
            "Locating requested tracks...",
            "Sifting through archives...",
            "Triangulating metadata...",
            "Database query in progress, sir.",
            "Running heuristic matching, sir.",
            "Cross-referencing index nodes...",
            "Analyzing storage, sir. Stand by.",
            "Searching indexing tables, sir.",
            "Parsing catalogs for matching entry, sir.",
            "Interrogating directories for metadata...",
            "Executing deep index lookup, sir.",
            "Querying indexing engines. One moment...",
            "Scanning database indexes, sir.",
            "Filtering collection matrices...",
            "Locating target nodes in high-dimensional index...",
            "Correlating structural identifiers...",
            "Interrogating local caches...",
            "Performing full sweep of the catalog, sir...",
            "Scanning active audio indexes, sir. Stand by...",
            "Tracing references across graphs...",
            "Heuristic database matching in progress...",
            "Searching library database coordinates...",
            "Executing real-time query, sir.",
            "Traversing matrices to find a match...",
            "Scanning high-dimensional matrices...",
            "Querying SQL indexes and cache...",
            "Scanning artist and album nodes...",
            "Searching high-fidelity registers, sir...",
            "Parsing databases for matching identifier...",
            "Checking active cache and indexes...",
            "Traversing local music graph coordinates, sir...",
            "Running matching algorithms...",
            "Interrogating acoustic feature tables...",
            "Searching index clusters, stand by...",
            "Traversing local repository tree...",
            "Analyzing spatial similarity matrices, sir. Stand by...",
            "Checking structural graph nodes...",
            "Filtering relational tables, sir...",
            "Iterating over database files...",
            "Verifying track signature patterns...",
            "Sweeping directories, sir...",
            "Executing deep search queries...",
            "Retrieving record from cache...",
            "Scanning collection coordinates..."
        ],
        "error": [
            "I've encountered a system error, sir.",
            "Action unsuccessful, sir.",
            "System is unresponsive to that request.",
            "Snag in the audio sub-system, sir.",
            "Logic circuits report a conflict, sir.",
            "Exception occurred in the internal pipeline, sir.",
            "Signal disruption detected. Command aborted.",
            "Warning: Audio controller returned an error, sir.",
            "Operation aborted due to an execution error.",
            "Apologies, sir. Systems report a command failure.",
            "Protocol collision detected. Please retry, sir.",
            "Unable to resolve requested instruction.",
            "Operational conflict in backend dispatcher, sir.",
            "Unable to execute. Hardware register fault.",
            "Exception in command dispatcher, sir.",
            "Audio bridge reported a registration fault.",
            "Apologies, sir. Instruction pipeline is blocked.",
            "Anomaly detected in music database response.",
            "Hardware registers are temporarily busy, sir.",
            "I cannot complete that procedure right now, sir.",
            "Diagnostics indicate a background task failure.",
            "Database returned empty status record.",
            "Routing pipeline returned status code error, sir.",
            "Data pipeline integrity fault detected.",
            "Apologies, sir. Unexpected error has halted request.",
            "Audio bridge is temporarily unresponsive, sir.",
            "Logic pipeline validation error. Aborting sequence.",
            "Diagnostics report a handler collision, sir.",
            "Unexpected file access fault, sir.",
            "Operational failure in music engine. Standing by.",
            "Exception occurred while parsing parameters.",
            "Backend dispatcher returned bad response, sir.",
            "Database controller failed to acknowledge write, sir.",
            "Exception detected in playback state machine.",
            "Warning: Indexing read collision, sir.",
            "Unable to process. System reported hardware busy.",
            "Command queue synchronization anomaly, sir.",
            "Operational parameters out-of-bounds.",
            "Audio driver reported state transition failure, sir.",
            "Apologies, sir. Instruction triggered null reference.",
            "Underlying audio service failed to initialize.",
            "Logic conflict addressing audio registers, sir.",
            "Unexpected callback timeout in audio bridge, sir.",
            "Command interrupted by system fault, sir."
        ],
        "not_found": [
            "I couldn't find a match for '{query}', sir.",
            "My apologies, but '{query}' is not in the library.",
            "'{query}' seems missing from the database.",
            "Search completed, but '{query}' remains elusive.",
            "I've searched the drive, but '{query}' is not here.",
            "No matching index nodes for '{query}', sir.",
            "Telemetry reports zero database hits for '{query}'.",
            "'{query}' is not registered in the catalog.",
            "No viable results matching '{query}', sir.",
            "Regrettably, '{query}' is not in your collection.",
            "Query yielded no matches for '{query}'.",
            "'{query}' is currently unavailable, sir.",
            "Indices contain no trace of '{query}'.",
            "Could not locate '{query}' in cache or index.",
            "Scanning active paths returned null for '{query}'.",
            "Search finished, but '{query}' remains undiscovered.",
            "Index contains no reference matching '{query}', sir.",
            "I've traversed the catalog, but found no traces of '{query}'.",
            "'{query}' is absent from local directories.",
            "Failed to locate '{query}' in storage nodes.",
            "Found nothing resembling '{query}' in the music graph.",
            "No index references matched '{query}', sir.",
            "Swept all indexing tables for '{query}' with zero results.",
            "I've swept the database, sir, but '{query}' is not registered.",
            "'{query}' doesn't exist in our music graph, sir.",
            "No files matching '{query}' in local storage.",
            "Index search for '{query}' returned zero nodes.",
            "Apologies, sir. '{query}' returned no results.",
            "Search heuristics returned empty set for '{query}'.",
            "No active cache or indexing references matched '{query}', sir.",
            "I swept all directories, but '{query}' is not present.",
            "'{query}' is not present in active collection.",
            "'{query}' is not in the database.",
            "Unable to locate '{query}' in library matrices.",
            "Coordinates did not yield hits for '{query}', sir.",
            "Indices show no record of '{query}'.",
            "Could not find '{query}' in metadata or file tables.",
            "Search pipeline returned null for '{query}', sir.",
            "No trace of '{query}' in structural graph nodes.",
            "Query for '{query}' returned zero matching files.",
            "My search indices have no entries for '{query}', sir.",
            "No matching entry for '{query}' in the database.",
            "I've combed the catalog, but '{query}' is missing."
        ],
        "unknown": [
            "I don't understand that command, sir.",
            "My apologies, sir, but that is not in my protocols.",
            "Could you rephrase that, sir?",
            "My training does not cover that phrasing, sir.",
            "I'm having trouble parsing that request, sir.",
            "Command unrecognized. Outside my vocabulary matrix, sir.",
            "Syntax mismatch. Could you express your intent differently?",
            "Pardon me, sir. I cannot map that statement to an action.",
            "I didn't grasp that command, sir. Could you clarify?",
            "Semantic analysis yielded zero high-confidence matches, sir.",
            "That query is outside my operational directives.",
            "Command context unclear, sir. Please rephrase.",
            "Unrecognized phrasing. Awaiting a different instruction, sir.",
            "That request lies outside my current vocabulary, sir.",
            "I didn't catch that, sir. Try a different format.",
            "My processor was unable to resolve that statement.",
            "Apologies, sir. That phrasing is not registered.",
            "I couldn't parse your last prompt, sir.",
            "Could you use a different phrasing, sir?",
            "Input patterns are outside my instruction matrix, sir.",
            "Parser error: Semantic intent remains highly ambiguous.",
            "Pardon me, sir, but that command is outside my syntax definitions.",
            "I didn't capture the intent behind that phrasing, sir.",
            "Semantic intent parsing returned low confidence.",
            "Could you clarify that, sir? Unable to map action.",
            "Command unrecognized, sir. No matches.",
            "I cannot match that request to my registered routines.",
            "Syntax error in the instruction, sir. Please rephrase.",
            "That falls outside my instruction guidelines, sir.",
            "Apologies, sir. Phrasing is not in my database.",
            "I didn't catch the action key in that, sir.",
            "I couldn't identify operational parameters in your request, sir.",
            "Semantic parsing failed. Try a simpler command structure, sir.",
            "My processor couldn't resolve that prompt, sir.",
            "That query exceeds my standard instruction protocol, sir.",
            "Could you restate your instruction, sir?",
            "I'm at your disposal, sir, but I didn't recognize that command.",
            "Command context is ambiguous. Please use clearer terms.",
            "Could not map that input to audio controller functions.",
            "That directive doesn't match any registered semantic flows, sir.",
            "Pardon me, sir. I am unable to decode that specific command."
        ],
        "playback_control": [
            "Of course, sir. {action}.",
            "As you wish. {action}.",
            "Understood. {action}.",
            "Right away. {action}.",
            "Adjusting output stream: {action}.",
            "Modifying stream variables. {action}, sir.",
            "Executing playback adjustment. {action}.",
            "Audio driver updated. {action}, sir.",
            "Delivered to audio pipeline: {action}.",
            "Playback registers updated: {action}.",
            "Signal path modified. {action}, sir.",
            "Routing transition. {action}.",
            "Modifying parameters. {action}.",
            "Synchronizing decks. {action}, sir.",
            "Instruction registered. {action}.",
            "Active signal line updated. {action}, sir.",
            "Updating playback state. {action}.",
            "Audio output updated. {action}, sir.",
            "State updated to: {action}.",
            "Instructing audio service: {action}, sir.",
            "Modifying registers: {action}.",
            "Audio engine updated: {action}, sir.",
            "Queue state modified. {action}.",
            "Understood. Applying {action}.",
            "Dispatched to music bridge: {action}, sir.",
            "Executing target transition: {action}.",
            "Updating active deck: {action}, sir.",
            "Understood. Performing {action}.",
            "Audio stream adjusted. {action}, sir.",
            "Playback parameters set: {action}.",
            "Directing controller to execute: {action}.",
            "Acknowledged. State changed: {action}, sir.",
            "Applying request to stream: {action}.",
            "Routing state update: {action}, sir.",
            "Deck parameters modified: {action}.",
            "Configuring music engine: {action}, sir.",
            "Transitioning audio system. {action}.",
            "Instructing player to: {action}, sir.",
            "Confirmed. Performing: {action}.",
            "Playback system updated to: {action}."
        ],
        "discovery": [
            "Initiating similarity sequence, sir.",
            "Accessing acoustic graph. One moment...",
            "Expanding playback horizon, sir.",
            "Cross-referencing signatures...",
            "Heuristics suggest this may suit your mood, sir.",
            "Analyzing sonic landscape. One moment...",
            "Calculating acoustic neighbors in library graph...",
            "Navigating acoustic edges for structural features...",
            "Comparing high-dimensional DSP features, sir...",
            "Synthesizing pathway matching the current vibe, sir.",
            "Locating tracks with adjacent properties, sir. Stand by...",
            "Traversing network to discover related tracks...",
            "Identifying acoustic matches for target signature, sir.",
            "Tracing optimal path through acoustic dimensions...",
            "Calculating distance between structural features, sir...",
            "Compiling list of correlated sonic properties...",
            "Generating customized walk across library graph...",
            "Cross-referencing similar mood coordinates...",
            "Walking acoustic edges for adjacent tracks...",
            "Searching spatial embedding regions, sir.",
            "Analyzing spectral similarities for graph proximity...",
            "Tracing signatures across database clusters...",
            "Locating tracks with similar vectors, sir...",
            "Traversing high-fidelity acoustic graph nodes...",
            "Executing distance queries in music graph...",
            "Finding sonic neighbors matching this track, sir...",
            "Analyzing rhythm attributes for adjacent tracks...",
            "Scanning features to build an adjacent walk...",
            "Evaluating structural similarities...",
            "Checking proximity matrices in music graph, sir...",
            "Synthesizing similar-vibed list from adjacent nodes...",
            "Navigating acoustic vectors for transition, sir...",
            "Traversing graph edges for matching moods...",
            "Searching spatial indexes for sonic neighbors...",
            "Correlating acoustic signatures to expand queue, sir...",
            "Walking graph connections for matching vibes...",
            "Calculating feature distances...",
            "Retrieving sonically aligned tracks...",
            "Parsing DSP features for matching signatures, sir...",
            "Tracing acoustic edges for seamless journey...",
            "Interrogating graph for similar acoustic properties, sir..."
        ],
        "status": [
            "Currently processing, sir.",
            "Systems are green. Playing: {track}.",
            "This is {track} by {artist}, sir.",
            "Telemetry reports listening to: {track}.",
            "Active deck: {track} by {artist}.",
            "We are streaming {track} by {artist}, sir.",
            "Playing {track} from your local library.",
            "Now playing: {track} by {artist}.",
            "Active signal path: {track} by {artist}.",
            "Diagnostics report playing: {track}.",
            "Current output: {track} by {artist}, sir.",
            "Outputting {track} by {artist}.",
            "Playback deck operational. Playing {track}.",
            "We are listening to {track} by {artist}, sir.",
            "Active track: {track} from local collection, sir.",
            "Active deck reporting: {track} by {artist}.",
            "Currently decoding: {track} by {artist}, sir.",
            "Stream active: {track} by {artist}.",
            "Telemetry indicates playing: {track} by {artist}, sir.",
            "Output active: {track} by {artist}.",
            "Current stream: {track} from local storage.",
            "Active deck streaming: {track} by {artist}, sir.",
            "Playback status: {track} by {artist}.",
            "Outputting: {track} by {artist}.",
            "Active channel: {track} by {artist}, sir.",
            "Decoders processing {track} by {artist}.",
            "Audio player playing {track}.",
            "Stream points to {track} by {artist}, sir.",
            "Deck details: {track} by {artist}.",
            "Audio service streaming {track} by {artist}.",
            "Active registers show: {track} by {artist}, sir.",
            "Playing {track} by {artist}.",
            "Now playing {track} by {artist}."
        ],
        "greeting": [
            "At your service, sir. Mapped library. How can I help?",
            "Systems online. Library graph indexed. How may I assist?",
            "Ready for your commands, sir. Music network is at your disposal.",
            "Good to see you, sir. Ready to manage your collection.",
            "All systems normal, sir. Audio matrix online.",
            "Jarvis operational. Ready to queue selections, sir.",
            "Acoustic graph loaded, sir. What shall we play?",
            "Standing by, sir. Awaiting your instruction.",
            "Ready to parse requests, sir. Speak when ready.",
            "Library registers optimized, sir. How may I serve you?",
            "Always a pleasure, sir. Database fully synced.",
            "Systems online. Graph and mood attributes mapped.",
            "Operational and standing by, sir. What shall we play?",
            "Jarvis online. Audio matrix and graphs initialized.",
            "Good day, sir. Catalog ready. Awaiting directive.",
            "Database online, DSP loaded. Ready for instructions, sir.",
            "{time_greeting}, sir. Music network is ready. What shall we play?",
            "{time_greeting}, sir. Mapped library. How may I assist you?",
            "{time_greeting}, sir. Systems online. Awaiting your instructions.",
            "{time_greeting}. Ready for commands, sir. Acoustic graph loaded.",
            "{time_greeting}, sir. Standing by to direct audio stream.",
            "{time_greeting}, sir. Jarvis operational. Library synced.",
            "{time_greeting}, sir. Standing by. Let me know what you want to hear.",
            "{time_greeting}, sir. Ready for your directive.",
            "{time_greeting}, sir. Library registers checked. What is your command?",
            "{time_greeting}, sir. All systems green. Ready to curate.",
            "{time_greeting}, sir. Music graph operational. How shall we begin?",
            "{time_greeting}, sir. Ready to parse and route commands.",
            "{time_greeting}, sir. Ready to direct stream. Tell me what is on your mind.",
            "{time_greeting}, sir. Decoders and index graphs ready.",
            "{time_greeting}, sir. Standing ready. Let's traverse your library.",
            "{time_greeting}, sir. Indexed matching nodes. Ready for instructions.",
            "{time_greeting}, sir. Active. How can I enhance your listening?",
            "{time_greeting}, sir. Command filters and parser are operational.",
            "{time_greeting}, sir. Systems aligned and ready to execute.",
            "{time_greeting}, sir. Caches primed and indexed. Command me."
        ],
        "disambiguation": [
            "I found {count} tracks matching '{query}', sir. Which one would you prefer?",
            "Multiple entries match '{query}', sir. Kindly select your preferred choice:",
            "Your collection contains {count} hits for '{query}', sir. Please choose from the options below:",
            "I've indexed {count} matches for '{query}', sir. Which shall we queue?"
        ],
        "mood_steer": [
            "{affirmative} Curated a {mood} mix for you, sir. Starting with '{title}' by {artist}.",
            "{affirmative} Assembled a {mood} selection, sir. Now playing '{title}' by {artist}.",
            "{affirmative} Tailored a {mood} listening flow for you, sir. Starting '{title}'.",
            "{affirmative} Calibrating audio dynamics for a {mood} aesthetic, sir. Playing '{title}'."
        ],
        "track_info": [
            "Currently playing '{title}' by {artist}, from the album '{album}'. Track duration is {duration}.",
            "Inspecting track registers, sir: '{title}' by {artist}, from '{album}' ({duration}).",
            "Telemetry reports playing '{title}' by {artist}, featured on '{album}'. Duration: {duration}.",
            "Active deck is streaming '{title}' by {artist}, from '{album}' ({duration}), sir."
        ],
        "queue_save": [
            "{affirmative} Saved current walk to playlist '{name}' ({count} tracks).",
            "{affirmative} Created playlist '{name}' with {count} walk tracks.",
            "{affirmative} Registered current walk as playlist '{name}' containing {count} tracks, sir."
        ],
        "queue_remove": [
            "{affirmative} Removed '{title}' by {artist} from the queue. {remaining} tracks remaining.",
            "{affirmative} Dropped '{title}' by {artist} from your active queue. {remaining} tracks left.",
            "{affirmative} Pruned '{title}' by {artist} from the queue stack. {remaining} items remain."
        ],
        "queue_move": [
            "{affirmative} Moved '{title}' to play next.",
            "{affirmative} Repositioned '{title}' to the top of the queue, sir.",
            "{affirmative} Shifted '{title}' to position #{pos} in your queue."
        ]
    }

    def _say(self, category: str, **kwargs) -> str:
        """Pick a random phrase from a category and format it with kwargs."""
        phrases = self.JARVIS_PHRASES.get(category, ["Yes?"])
        phrase = random.choice(phrases)
        if category == "greeting" and "{time_greeting}" in phrase:
            import datetime
            hour = datetime.datetime.now().hour
            if hour < 12:
                time_greet = "Good morning"
            elif hour < 17:
                time_greet = "Good afternoon"
            else:
                time_greet = "Good evening"
            kwargs["time_greeting"] = time_greet
        return phrase.format(**kwargs)

    # ── Public dispatch ─────────────────────────────────────────────────────

    async def dispatch(self, intent: ai.Intent,
                       history_provider: Optional[Callable[[], list]] = None,
                       ) -> AssistantResponse:
        """Route an Intent to its handler. Catches and reports handler errors
        so the chat UI always gets a renderable response.

        If a confirmation is pending, INTENT_AFFIRMATIVE resolves it (running
        the queued action), INTENT_NEGATIVE cancels it, and anything else
        clears the pending and routes normally as a new request — the user
        moved on, treat their input as a fresh intent rather than ambiguously
        re-asking.

        history_provider, when supplied, returns the in-memory chat history
        list — avoids the disk round-trip through ChatMemoryManager."""
        self._history_cache = None
        if history_provider is not None:
            self._history_provider = history_provider
        try:
            response = await self._dispatch_inner(intent)
            if response is not None:
                response.intent = intent
                self._announce_anaphora(intent, response)
            return response
        finally:
            self._history_cache = None
            self._history_provider = None

    def _announce_anaphora(self, intent: "ai.Intent", response: "AssistantResponse") -> None:
        """If _resolve_anaphora rewrote the query, prepend an audible
        acknowledgement to response.spoken so the user can tell that Jarvis
        is tracking context (rather than getting lucky). Bubble text is
        already self-evidently the resolved entity — no need to touch it."""
        label = intent.extras.get("anaphora_resolved_label")
        trigger = intent.extras.get("anaphora_trigger")
        if not label or not response.success:
            return
        if trigger:
            # Explicit pronoun: prepend a short acknowledgement.
            prefix = f"Resolving '{trigger}' to {label}. "
            response.spoken = prefix + (response.spoken or "")
        else:
            # Implicit-context intent (play_similar / play_more_by with no
            # pronoun). Softer phrasing — append as a tail clause so the
            # primary handler phrase still reads naturally.
            tail = f" (based on our recent discussion of {label})"
            if response.spoken and not response.spoken.endswith(tail):
                response.spoken = response.spoken.rstrip(".") + "." + tail

    async def _dispatch_inner(self, intent: ai.Intent) -> AssistantResponse:

        # Resolve pending multi-choice dialog first.
        pending_choice = self._pending_choice
        if pending_choice is not None:
            if intent.name == ai.INTENT_NEGATIVE:
                self._pending_choice = None
                return AssistantResponse(
                    spoken="Understood, sir. Selection cancelled.",
                    displayed="Selection cancelled.",
                )
            selected_option = None
            q_raw = (intent.query or intent.raw or "").strip().lower()
            digit_map = {
                "1": 0, "one": 0, "first": 0,
                "2": 1, "two": 1, "second": 1,
                "3": 2, "three": 2, "third": 2,
                "4": 3, "four": 3, "fourth": 3,
                "5": 4, "five": 4, "fifth": 4,
            }
            idx = digit_map.get(q_raw)
            if idx is not None and 0 <= idx < len(pending_choice.options):
                selected_option = pending_choice.options[idx]
            else:
                for opt in pending_choice.options:
                    if q_raw in opt.title.lower() or (opt.subtitle and q_raw in opt.subtitle.lower()):
                        selected_option = opt
                        break
            if selected_option is not None:
                self._pending_choice = None
                if pending_choice.on_select_callback:
                    try:
                        return await pending_choice.on_select_callback(selected_option)
                    except Exception as e:
                        logger.exception("PendingChoice: on_select_callback failed")
                        return AssistantResponse(
                            spoken="I had trouble processing that choice, sir.",
                            displayed=f"Error executing choice callback: {e}",
                            success=False,
                        )
                return AssistantResponse(
                    spoken=f"Selection confirmed: {selected_option.title}.",
                    displayed=f"Selected: **{selected_option.title}**",
                )
            # Anything else: drop pending choice and route normally as a fresh turn.
            self._pending_choice = None

        # Resolve pending confirmation first.
        pending = self._pending
        if pending is not None:
            if intent.name == ai.INTENT_AFFIRMATIVE:
                self._pending = None
                if pending.on_yes_callback is not None:
                    try:
                        return await pending.on_yes_callback()
                    except Exception as e:
                        logger.exception("PendingConfirmation: on_yes_callback failed")
                        return AssistantResponse(
                            spoken="I had trouble processing that, sir.",
                            displayed=f"Error executing callback: {e}",
                            success=False,
                        )
                return AssistantResponse(
                    spoken=pending.on_yes_msg,
                    displayed=pending.on_yes_msg,
                    action=pending.on_yes_action,
                )
            if intent.name == ai.INTENT_NEGATIVE:
                self._pending = None
                if pending.on_no_callback is not None:
                    try:
                        return await pending.on_no_callback()
                    except Exception as e:
                        logger.exception("PendingConfirmation: on_no_callback failed")
                        return AssistantResponse(
                            spoken="I had trouble processing that, sir.",
                            displayed=f"Error executing callback: {e}",
                            success=False,
                        )
                return AssistantResponse(
                    spoken=pending.on_no_msg,
                    displayed=pending.on_no_msg,
                )
            # Anything else: drop pending, fall through to normal dispatch.
            self._pending = None

        # Resolve anaphoric references / pronouns before dispatching so all
        # downstream handlers receive a concrete, validated query.
        intent, confirm_q = await self._resolve_anaphora(intent)

        # When the resolved entity was found far back in history (> threshold
        # messages), _resolve_anaphora defers commitment and returns a question
        # instead.  Arm _pending so the next affirmative/negative is handled,
        # then return the question bubble immediately.
        if confirm_q is not None:
            resolved_intent = intent          # already fully resolved
            # Ambiguity path: 'no' switches to the runner-up instead of
            # cancelling outright. The alt intent was built by _resolve_anaphora.
            alt_intent = intent.extras.pop("anaphora_alt_intent", None)
            alt_label = intent.extras.pop("anaphora_alt_label", None)
            if alt_intent is not None:
                no_callback = lambda: self.dispatch(alt_intent)
                no_msg = (f"Understood, sir. Going with **{alt_label}** instead."
                          if alt_label else "Understood, sir. Going with the alternative.")
            else:
                no_callback = None
                no_msg = "Understood, sir. Standing by."
            self._pending = PendingConfirmation(
                prompt=confirm_q,
                on_yes_msg=confirm_q,         # unused; callback drives the reply
                on_no_msg=no_msg,
                on_yes_callback=lambda: self.dispatch(resolved_intent),
                on_no_callback=no_callback,
            )
            return AssistantResponse(
                spoken=confirm_q,
                displayed=confirm_q,
            )

        try:
            # Instant offline execution for basic media controls (<5ms execution)
            FAST_DETERMINISTIC_INTENTS = {
                ai.INTENT_SKIP, ai.INTENT_PREV, ai.INTENT_PAUSE, ai.INTENT_RESUME,
                ai.INTENT_STOP, ai.INTENT_CLEAR_QUEUE, ai.INTENT_SHUFFLE,
                ai.INTENT_MUTE, ai.INTENT_UNMUTE
            }
            if intent.name in FAST_DETERMINISTIC_INTENTS:
                handler = self._INTENT_DISPATCH.get(intent.name, AssistantRunner._handle_unknown)
                return await handler(self, intent)

            # Complex / conversational / discovery queries route through LLM engine if enabled
            return await self._dispatch_llm(intent)
        except Exception as exc:
            logger.exception("AssistantRunner: handler failed for %s", intent.name)
            return AssistantResponse(
                spoken=self._say("error"),
                displayed=f"Error: {exc}",
                success=False,
            )

    # ── AI agent (in-process LLM tool-calling) ──────────────────────────────

    def _load_assistant_cfg(self):
        """Return the cached AssistantConfig, re-parsing only when the on-disk
        config changes (keyed by file mtime). Avoids re-reading + re-serialising
        the whole TOML on every agent turn."""
        try:
            import os
            from utils.streamrip_api import get_config_path
            path = get_config_path()
            mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
        except Exception:
            mtime = 0.0
        if self._cfg_cache is not None and mtime == self._cfg_mtime:
            return self._cfg_cache
        try:
            from utils.config import ConfigData
            from utils.streamrip_api import load_config
            from tomlkit import dumps
            cfg_dict = load_config()
            cfg = ConfigData.from_toml(dumps(cfg_dict)) if isinstance(cfg_dict, dict) else None
        except Exception:
            cfg = None
        self._cfg_cache = cfg
        self._cfg_mtime = mtime
        return cfg

    def agent_reset(self) -> None:
        """Clear per-turn agent-loop state before an LLM tool-calling pass."""
        self._agent_deferred_play = False
        self._agent_interrupt = None
        self._agent_last_extras = {}
        self._agent_tools_used = []

    async def _execute_intent(self, intent: ai.Intent) -> AssistantResponse:
        """Run one intent through its leaf handler directly, bypassing the LLM
        branch and the pending/anaphora machinery. This is the bridge agent
        tools use to reuse the proven deterministic handlers."""
        handler = self._INTENT_DISPATCH.get(intent.name, AssistantRunner._handle_unknown)
        return await handler(self, intent)

    def _absorb_agent_response(self, resp: AssistantResponse) -> None:
        """Fold a bridged handler's response into agent-loop state: carry the
        deferred-play flag forward (the view starts audio after TTS), remember
        entities for anaphora, and short-circuit the loop when the handler armed
        a disambiguation choice or confirmation that needs the user."""
        if resp is None:
            return
        if resp.deferred_play:
            self._agent_deferred_play = True
        if resp.extras:
            self._agent_last_extras = resp.extras
        if self._pending_choice is not None or self._pending is not None:
            self._agent_interrupt = resp

    async def agent_run_intent(self, intent_name: str, query: Optional[str] = None,
                               extras: Optional[dict] = None) -> dict:
        """Bridge an agent tool call to a runner handler, absorb its side
        effects, and return a compact JSON-serialisable result for the model."""
        intent = ai.Intent(name=intent_name, query=query, raw=query or "", extras=extras or {})
        resp = await self._execute_intent(intent)
        self._absorb_agent_response(resp)
        from utils.llm_tools import strip_markdown
        result: dict = {"success": resp.success, "message": strip_markdown(resp.displayed)}
        if resp.extras.get("options"):
            result["options"] = resp.extras["options"]
            result["awaiting_choice"] = True
        return result

    def _stage_tracks(self, tracks: list[dict], label: str) -> AssistantResponse:
        """Replace the queue with an explicit ordered track list (album /
        playlist playback). Stages only — the view starts audio after TTS."""
        engine_tracks = [_to_engine_track(t) for t in tracks]
        self.engine.set_queue(engine_tracks, start_index=0)
        if engine_tracks:
            self._remember(engine_tracks[0]["path"])
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Playing {label}.",
            displayed=f"Playing **{label}** ({len(engine_tracks)} tracks).",
            extras={"track": tracks[0] if tracks else None, "queued": len(engine_tracks)},
            deferred_play=True,
        )

    async def agent_play_tracks(self, tracks: list[dict], label: str) -> dict:
        """Bridge for album/playlist playback: stage an explicit list now and
        absorb the deferred-play flag so the view starts audio after TTS."""
        if not tracks:
            return {"success": False, "message": f"No tracks found for {label}."}
        resp = self._stage_tracks(tracks, label)
        self._absorb_agent_response(resp)
        from utils.llm_tools import strip_markdown
        return {"success": True, "message": strip_markdown(resp.displayed)}

    def agent_request_confirmation(self, spoken: str, displayed: str,
                                   on_yes: Callable) -> dict:
        """Arm a yes/no confirmation for a destructive/outward tool action and
        stop the agent loop so the prompt is surfaced deterministically. The
        real action (`on_yes`, an async callable returning an AssistantResponse)
        runs only when the user affirms on the next turn."""
        from utils.llm_tools import strip_markdown
        self._pending = PendingConfirmation(
            prompt=spoken,
            on_yes_msg=spoken,
            on_no_msg="Understood, sir. Cancelled.",
            on_yes_callback=on_yes,
        )
        self._agent_interrupt = AssistantResponse(spoken=spoken, displayed=displayed)
        return {"success": True, "awaiting_confirmation": True,
                "message": strip_markdown(displayed)}

    def _history_snapshot(self) -> list:
        """Single source of chat history for the agent: the live in-memory list
        when the view injected one, else a disk read. No double-counting."""
        if self._history_provider is not None:
            try:
                return list(self._history_provider() or [])
            except Exception:
                pass
        try:
            from utils.chat_memory import ChatMemoryManager
            return ChatMemoryManager().load_session().get("messages", [])
        except Exception:
            return []

    async def _agent_context_line(self) -> str:
        """A short live-context line injected into the agent's system prompt so
        trivial 'this song / my library' questions need no tool round-trip."""
        parts: list[str] = []
        try:
            path = getattr(self.engine, "current_path", "") or ""
            if path:
                t = await self.db.get_track_full(path)
                if t:
                    head = " — ".join(b for b in (t.get("title"), t.get("artist")) if b)
                    if head:
                        extra = [x for x in (t.get("album"), t.get("genre")) if x]
                        suffix = f" ({', '.join(extra)})" if extra else ""
                        parts.append(f"Now playing: {head}{suffix}.")
        except Exception:
            pass
        try:
            total = await self.db.get_total_tracks()
            if total:
                parts.append(f"Local library holds {total} tracks.")
        except Exception:
            pass
        return " ".join(parts)

    def _finalize_agent(self, reply_markdown: str) -> AssistantResponse:
        """Wrap the agent's final text into a response: markdown for the bubble,
        stripped plain text for TTS, plus carried playback/entity/provenance."""
        from utils.llm_tools import strip_markdown
        resp = AssistantResponse(
            spoken=strip_markdown(reply_markdown) or "At your service, sir.",
            displayed=reply_markdown,
            deferred_play=self._agent_deferred_play,
        )
        resp.extras = dict(self._agent_last_extras or {})
        resp.extras["agent"] = {"used_llm": True, "tools": list(self._agent_tools_used)}
        return resp

    def _finalize_agent_interrupt(self) -> AssistantResponse:
        """Return the disambiguation/confirmation prompt a tool raised, tagged
        with agent provenance so the bubble shows the AI-agent stage."""
        resp = self._agent_interrupt
        if resp.extras is None:
            resp.extras = {}
        resp.extras.setdefault("agent", {"used_llm": True, "tools": list(self._agent_tools_used)})
        return resp

    async def _dispatch_llm(self, intent: ai.Intent) -> AssistantResponse:
        """Route a request through the in-process AI agent — an LLM tool-calling
        loop over the runner's music tools. Falls back to the deterministic
        handler whenever the agent is disabled, unconfigured, or errors."""
        cfg = self._load_assistant_cfg()
        acfg = cfg.assistant if (cfg and hasattr(cfg, "assistant")) else None

        if acfg is not None and not acfg.llm_enabled:
            return await self._execute_intent(intent)

        provider = acfg.llm_provider if acfg else "gemini"
        from utils.llm_engine import LLMEngine
        engine = LLMEngine(
            provider=provider,
            api_key=acfg.gemini_api_key if acfg else "",
            model=acfg.gemini_model if acfg else "gemini-2.5-flash",
            ollama_endpoint=acfg.ollama_endpoint if acfg else "http://localhost:11434/v1",
            ollama_model=acfg.ollama_model if acfg else "llama3.2",
        )

        if not engine.is_configured():
            # No key / local server: fall back to deterministic parsing for
            # known intents; only nag on a truly unrecognised utterance.
            if intent.name != ai.INTENT_UNKNOWN:
                return await self._execute_intent(intent)
            return AssistantResponse(
                spoken="Please configure your free Gemini API key in Settings, AI Assistant, to activate my full intelligence, sir.",
                displayed="**AI Agent unconfigured**: paste your free API key from [Google AI Studio](https://aistudio.google.com/) under **Settings → AI Assistant** to enable conversational AI & smart curation.",
                success=False,
            )

        self.agent_reset()
        from utils.llm_tools import get_agent_tools, execute_tool

        system_prompt = (
            "You are Jarvis, a concise, sophisticated AI assistant embedded in the Mai An Lab "
            "high-fidelity music app. Address the user as 'sir'. You can both KNOW and ACT on the "
            "user's library via tools: read tools (library overview & stats, top-played, artists, "
            "an artist's albums, an album's tracks, playlists and their contents, recently played, "
            "and rich per-track details incl. bpm/energy) and action tools (search, play/enqueue/"
            "play-next, play whole albums or playlists, the DSP similarity walk, mood steering, "
            "transport control, saving playlists, and Qobuz online search). "
            "Answer library questions by calling the read tools and reasoning over the results — "
            "e.g. counts, 'most played', 'how many by X', 'what's on this album'. Chain tools when "
            "needed (look up, then act). Only reference tracks the tools actually return — never "
            "invent library paths. When a tool reports it is awaiting the user's confirmation or "
            "choice, stop and let the user answer. Keep spoken replies short and natural (read aloud "
            "by text-to-speech)."
        )
        ctx = await self._agent_context_line()
        if ctx:
            system_prompt = f"{system_prompt}\n\nLive context — {ctx}"

        messages = [{"role": "system", "content": system_prompt}]
        history = self._history_snapshot()
        for m in history[-10:]:
            role = "user" if m.get("sender") == "user" else "assistant"
            messages.append({"role": role, "content": m.get("text", "")})

        raw_query = (intent.raw or intent.query or "").strip()
        # The view appends the user turn to history *before* dispatch, so it may
        # already be the last message — don't feed it to the model twice.
        if not (len(messages) > 1 and messages[-1]["role"] == "user"
                and (messages[-1]["content"] or "").strip() == raw_query):
            messages.append({"role": "user", "content": raw_query})

        tools = get_agent_tools()
        import json
        res = None
        for _ in range(4):
            res = await engine.chat_completion(messages, tools=tools)
            if not res.success:
                logger.warning("Agent LLM call failed: %s", res.error_message)
                if intent.name != ai.INTENT_UNKNOWN:
                    return await self._execute_intent(intent)
                return AssistantResponse(
                    spoken=f"My apologies, sir. The connection to {provider} failed.",
                    displayed=f"Error connecting to AI provider ({provider}): {res.error_message}",
                    success=False,
                )

            if not res.tool_calls:
                return self._finalize_agent(res.content or "At your service, sir.")

            # Echo the provider's raw assistant message verbatim so Gemini
            # thinking models get their thought_signature back (a reconstructed
            # message drops it and the next turn 400s). Fall back to a rebuilt
            # message only if the provider gave us nothing raw.
            if res.raw_message:
                assistant_msg = dict(res.raw_message)
                assistant_msg.setdefault("role", "assistant")
            else:
                assistant_msg = {
                    "role": "assistant",
                    "content": res.content or "",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                        }
                        for tc in res.tool_calls
                    ],
                }
            messages.append(assistant_msg)

            for tc in res.tool_calls:
                tool_res = await execute_tool(tc["name"], tc["args"], self)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_res),
                })

            # A tool asked for confirmation or disambiguation: surface it now,
            # bypassing further LLM turns.
            if self._agent_interrupt is not None:
                return self._finalize_agent_interrupt()

        return self._finalize_agent(res.content if (res and res.content) else "Task executed successfully, sir.")


    async def dispatch_text(self, text: str,
                            history_provider: Optional[Callable[[], list]] = None,
                            ) -> AssistantResponse:
        """Convenience: parse + dispatch in one call. When the AI agent is
        enabled, skip the BGE semantic stage on a regex miss — the LLM handles
        those turns, so an embedding classify would be wasted work."""
        cfg = self._load_assistant_cfg()
        agent_on = bool(cfg.assistant.llm_enabled) if (cfg and hasattr(cfg, "assistant")) else False
        intent = ai.parse(text, semantic_fallback=not agent_on)
        return await self.dispatch(intent, history_provider=history_provider)

    # ── Recent-playback tracking ────────────────────────────────────────────

    def _remember(self, path: str, seed_path: Optional[str] = None) -> None:
        """Register a path as recently enqueued by the assistant. Updates the
        in-memory cap and fires a fire-and-forget DB write so the long-term
        avoid set (used by the random walk across app restarts) and the
        long-term avoid set (used by the random walk) accumulate state
        even when the assistant doesn't see playback completion events."""
        if not path:
            return
        if path in self._recent:
            self._recent.remove(path)
        self._recent.append(path)
        if len(self._recent) > self._recent_cap:
            self._recent.pop(0)
        # Persist async; we don't want a slow disk write to block dispatch.
        try:
            import asyncio as _aio
            _aio.create_task(
                self.db.record_playback(path, "played", seed_path=seed_path)
            )
        except Exception:
            pass

    async def _avoid_set(self) -> set[str]:
        """Union of the in-session recent list and the on-disk 7-day window.
        Async because it does one cached-cheap DB read; callers `await` it."""
        out = set(self._recent)
        try:
            out |= await self.db.recent_played_paths(window_seconds=7 * 86400)
        except Exception:
            # Persistent history is an optimisation; don't fail dispatch if
            # the table isn't initialised yet (fresh installs).
            pass
        return out

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

    # ── Anaphora / Conversational-Memory Resolution ─────────────────────────

    # Pronoun / noun-phrase → entity type.  Each trigger is type information:
    #   "it" / "the song" / "this"   → track
    #   "them" / "the band"          → artist
    #   "the album"                  → album
    #   "those" / "the tracks"       → multi (playlist / queue)
    _PRONOUN_TYPE: dict[str, str] = {
        # track-typed
        "it":              "track",
        "this":            "track",
        "that":            "track",
        "the song":        "track",
        "the track":       "track",
        "the music":       "track",
        "this song":       "track",
        "that song":       "track",
        "this one":        "track",
        "that one":        "track",
        # artist-typed
        "them":            "artist",
        "they":            "artist",
        "their":           "artist",
        "the band":        "artist",
        "the artist":      "artist",
        "the same artist": "artist",
        # album-typed
        "the album":       "album",
        "this album":      "album",
        # multi-typed (playlist / queue)
        "those":           "multi",
        "these":           "multi",
        "the tracks":      "multi",
        "those tracks":    "multi",
    }

    # Derived for backwards-compat (existing tests reference this name).
    _ANAPHORA_TRIGGERS = frozenset(_PRONOUN_TYPE)

    # Longest-first alternation so multi-word phrases beat single words.
    _ANAPHORA_RE = re.compile(
        r"\b("
        r"the\s+same\s+artist"
        r"|those\s+tracks"
        r"|this\s+album"
        r"|(?:this|that)\s+(?:song|track|one)"
        r"|the\s+(?:song|track|tracks|artist|band|album|music)"
        r"|it|this|that|those|these|them|they|their"
        r")\b",
        re.IGNORECASE,
    )

    # For implicit-context intents (no pronoun in query), the intent name
    # itself implies which entity type is needed.
    _INTENT_DEFAULT_TYPE: dict[str, str] = {
        "play_similar":  "track",
        "play_more_by":  "artist",
    }

    # Weight applied when picking the best candidate. Active-playback entities
    # (the user actually asked to play this) outweigh passive search / info
    # entities by ~2 ranks of recency — so 'play it' after a brief search
    # interlude still resolves to the thing that was *playing*, not the thing
    # that was *searched*. See _ANAPHORA_INTENT_K below.
    _INTENT_WEIGHT: dict[str, float] = {
        # active playback
        "play_now":       1.0,
        "queue_add":      1.0,
        "queue_next":     1.0,
        "play_similar":   1.0,
        "play_more_by":   1.0,
        "play_the_usual": 1.0,
        # passive / informational
        "search_artist":  0.5,
        "search_track":   0.5,
        "search_album":   0.5,
    }
    _INTENT_DEFAULT_WEIGHT = 0.3   # bubbles whose intent is missing/unknown
    # Multiplier on the weight delta in the candidate-picking score. With the
    # weights above, an active bubble overcomes ~2 ranks of recency advantage.
    _ANAPHORA_INTENT_K = 5.0

    # Intents for which we should attempt anaphora resolution even when the
    # query itself doesn't contain an explicit trigger word — because they
    # always act on whatever was last mentioned / currently playing.
    _IMPLICIT_ANAPHORA_INTENTS = frozenset([
        "play_similar", "play_more_by",
    ])

    # Number of messages back beyond which the resolver asks for confirmation
    # rather than acting silently.  Keeps references within ~3 exchange pairs
    # automatic; anything older gets a "Did you mean…?" check.
    # Confirmation now fires at the edge of the scan window rather than mid-chat.
    # Real conversations interleave smalltalk; a strict 5-message cutoff turned
    # "play it" 3 turns later into a confirmation prompt every single time.
    _ANAPHORA_CONFIRM_THRESHOLD = 12
    # When two competing candidates of the right type sit within this rank gap,
    # treat the situation as ambiguous and ask the user to pick rather than
    # silently going with the slightly-more-recent one.
    _ANAPHORA_AMBIGUITY_GAP = 2
    # How far back in the chat history we look for entity candidates. 12 was
    # the original cap; real conversations interleave music with smalltalk
    # and burn through that quickly. 25 covers ~12 conversational turns.
    _ANAPHORA_SCAN_WINDOW = 25

    async def _resolve_anaphora(self, intent: ai.Intent) -> tuple[ai.Intent, str | None]:
        """Attempt to resolve pronouns / anaphoric references in *intent* using
        structured entity data stored in the persistent chat history.

        The pronoun (trigger word) is treated as **type information** and
        enforces a **hard filter** during the history scan:

          - "it" / "the song" / "this one"  → only look for track entities
          - "them" / "the band" / "their"   → only look for artist entities
          - "the album"                     → only look for album entities

        If the pronoun says "them" but no artist appears in recent history the
        intent is returned unchanged — we never fall back to a different entity
        type, because doing so produces wrong results more often than not.

        For implicit-context intents (``play_similar``, ``play_more_by``) with
        no explicit pronoun, ``_INTENT_DEFAULT_TYPE`` provides the type hint.

        Scans at most the last 12 messages and picks the most recent matching
        entity (lowest recency rank).  When the winning entity is further than
        ``_ANAPHORA_CONFIRM_THRESHOLD`` messages back, the intent is fully
        resolved but a confirmation question is returned alongside it rather
        than silently acting — the caller (``dispatch``) arms ``_pending`` and
        Jarvis asks first.

        Returns ``(intent, confirm_question | None)``; never raises.
        """
        query_lower = (intent.query or "").strip().lower()

        # -- Determine hint type from pronoun trigger -------------------------
        m = self._ANAPHORA_RE.search(query_lower)
        trigger = re.sub(r"\s+", " ", m.group(0).strip().lower()) if m else None
        hint_type: str | None = self._PRONOUN_TYPE.get(trigger) if trigger else None

        # Implicit-intent path: intents that always need context from history.
        needs_context = (
            intent.name in self._IMPLICIT_ANAPHORA_INTENTS
            and not self.engine.current_path
        )

        if not trigger and not needs_context:
            return intent, None

        # For implicit intents without a pronoun, infer hint_type from intent.
        if hint_type is None and needs_context:
            hint_type = self._INTENT_DEFAULT_TYPE.get(intent.name)

        # -- 1. Load chat history -------------------------------------------
        # Prefer the in-memory provider injected by the caller (AssistantView
        # passes its live _history_list). Falls back to ChatMemoryManager's
        # disk session when no provider is supplied — tests rely on this.
        try:
            if self._history_cache is not None:
                session = self._history_cache
            elif self._history_provider is not None:
                session = {"messages": list(self._history_provider() or [])}
                self._history_cache = session
            else:
                from utils.chat_memory import ChatMemoryManager
                session = ChatMemoryManager().load_session()
                self._history_cache = session
            messages = session.get("messages", [])
        except Exception:
            logger.debug("_resolve_anaphora: could not load chat history -- skipping.")
            return intent, None

        if not messages:
            return intent, None

        # -- 2. Skip the current user turn (already appended before dispatch) -
        scan_msgs = list(reversed(messages))[:self._ANAPHORA_SCAN_WINDOW]
        if scan_msgs and scan_msgs[0].get("sender") == "user":
            scan_msgs = scan_msgs[1:]

        # -- 3. Collect typed candidates from history -------------------------
        _BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")   # legacy fallback only

        # Each entry: (rank, type_label, data, source_intent | None)
        # type_label ∈ {"track", "artist", "album", "track_legacy", "artist_legacy"}
        # source_intent comes from entities["intent"] when present — used by
        # the picker to weight active-playback bubbles over passive ones.
        candidates: list[tuple[int, str, object, str | None]] = []

        for rank, msg in enumerate(scan_msgs):
            if msg.get("sender") != "assistant":
                continue

            entities = msg.get("entities")
            if entities:
                src_intent = entities.get("intent")
                # Fast path: structured entity dict present.
                if (track := entities.get("track")) and track.get("path"):
                    candidates.append((rank, "track", track, src_intent))
                if artist := entities.get("artist"):
                    candidates.append((rank, "artist", artist, src_intent))
                if album := entities.get("album"):
                    candidates.append((rank, "album", album, src_intent))
            else:
                # Legacy fallback: parse bold markdown from pre-entities messages.
                text = msg.get("text", "")
                bold_hits = _BOLD_RE.findall(text)
                leg_title:  str | None = None
                leg_artist: str | None = None
                for hit in bold_hits:
                    if " — " in hit or " - " in hit:
                        sep = " — " if " — " in hit else " - "
                        parts = hit.split(sep, 1)
                        if len(parts) == 2:
                            leg_title  = leg_title  or parts[0].strip()
                            leg_artist = leg_artist or parts[1].strip()
                        else:
                            leg_title = leg_title or hit.strip()
                    else:
                        if leg_title and not leg_artist:
                            leg_artist = hit.strip()
                        else:
                            leg_title = leg_title or hit.strip()

                if leg_title:
                    candidates.append((rank, "track_legacy", (leg_title, leg_artist), None))
                elif leg_artist:
                    candidates.append((rank, "artist_legacy", leg_artist, None))

        # -- 4. Hard-filter: only consider entities of the hinted type --------
        if hint_type == "track":
            pool = [c for c in candidates if c[1] in ("track", "track_legacy")]
        elif hint_type == "artist":
            pool = [c for c in candidates if c[1] in ("artist", "artist_legacy")]
        elif hint_type == "album":
            pool = [c for c in candidates if c[1] == "album"]
        else:
            # "multi" or unknown hint — no resolution path yet.
            pool = []

        if not pool:
            logger.debug(
                "_resolve_anaphora: trigger '%s' (hint_type='%s') but no matching "
                "entity in history — leaving intent unchanged.",
                trigger, hint_type,
            )
            return intent, None

        # -- 5. Rank candidates by an intent-weighted recency score.
        # score = -rank + weight * K  →  more recent and/or more-active
        # bubbles score higher. Ambiguity is still measured by rank distance
        # (handled below), so a high-score active far from a high-score
        # passive does not produce a confirmation prompt.
        def _score(c: tuple) -> float:
            rank, _type, _data, src_intent = c
            weight = self._INTENT_WEIGHT.get(src_intent or "", self._INTENT_DEFAULT_WEIGHT)
            return -rank + weight * self._ANAPHORA_INTENT_K

        sorted_pool = sorted(pool, key=lambda c: (-_score(c), c[0]))
        best_rank = sorted_pool[0][0]
        intent.extras["resolved_via_intent"] = sorted_pool[0][3] or ""

        primary_label = await self._apply_anaphora_resolution(
            intent, sorted_pool[0], needs_context,
        )
        if primary_label is None:
            logger.debug("_resolve_anaphora: best candidate failed to resolve.")
            return intent, None
        logger.info("_resolve_anaphora: '%s' -> '%s'", query_lower, primary_label)

        # Stash for the speech wrapper in dispatch(): lets Jarvis audibly
        # acknowledge which prior entity got picked, so context-tracking is
        # observable to the user instead of feeling like a coincidence.
        intent.extras["anaphora_resolved_label"] = primary_label
        intent.extras["anaphora_trigger"] = trigger or ""

        # -- 6. Detect ambiguity: a second candidate close in rank with a
        # distinct label. We never silently pick between two equally-recent
        # entities — that's where wrong resolutions hurt the most.
        alt_intent = None
        alt_label = None
        if len(sorted_pool) >= 2:
            alt_candidate = sorted_pool[1]
            if alt_candidate[0] - best_rank <= self._ANAPHORA_AMBIGUITY_GAP:
                alt_intent = copy.copy(intent)
                # Reset to original state for an isolated resolution attempt.
                alt_intent.extras = {k: v for k, v in intent.extras.items()
                                     if k not in ("resolved_track",
                                                  "seed_path_override",
                                                  "seed_artist_override")}
                alt_intent.query = intent.raw or query_lower
                candidate_label = await self._apply_anaphora_resolution(
                    alt_intent, alt_candidate, needs_context,
                )
                if (candidate_label is None
                        or candidate_label.lower() == primary_label.lower()):
                    alt_intent = None      # same entity → not actually ambiguous
                else:
                    alt_label = candidate_label

        # -- 7. Decide whether to ask for confirmation ------------------------
        # Three paths:
        #   * ambiguous → "Did you mean X or Y" (yes=X, no=Y)
        #   * far back  → "Just to confirm, sir — did you mean X?"
        #   * otherwise → silent commit
        confirm_q: str | None = None
        if alt_intent is not None and alt_label is not None:
            confirm_q = (
                f"Did you mean **{primary_label}**, sir? "
                f"(say 'no' for **{alt_label}** instead)"
            )
            intent.extras["anaphora_alt_intent"] = alt_intent
            intent.extras["anaphora_alt_label"] = alt_label
            logger.info(
                "_resolve_anaphora: ambiguity between '%s' (rank %d) and '%s' (rank %d)",
                primary_label, best_rank, alt_label, sorted_pool[1][0],
            )
        elif best_rank > self._ANAPHORA_CONFIRM_THRESHOLD:
            confirm_q = f"Just to confirm, sir — did you mean **{primary_label}**?"

        return intent, confirm_q

    async def _apply_anaphora_resolution(
        self,
        target_intent: "ai.Intent",
        candidate: tuple,
        needs_context: bool,
    ) -> str | None:
        """Resolve *candidate* into a concrete track/artist and mutate
        *target_intent* (query + extras, plus engine-seed overrides when the
        intent needs implicit context).

        Returns a human-readable label for the resolved entity (e.g.
        "Yesterday by The Beatles" or "Pink Floyd"), or None when resolution
        failed. Used by both the primary candidate and the ambiguity check
        on the runner-up, hence the helper."""
        _, cand_type, cand_data, _src_intent = candidate
        resolved_track: dict | None = None
        resolved_artist: str | None = None

        if cand_type == "track":
            resolved_track = cand_data
        elif cand_type == "track_legacy":
            leg_title, leg_artist = cand_data
            search_q = (f"{leg_title} {leg_artist}".strip()
                        if leg_artist else leg_title)
            try:
                resolved_track = await self._resolve_query(search_q)
            except Exception as exc:
                logger.debug("_apply_anaphora_resolution: legacy track resolve failed: %s", exc)
        elif cand_type == "artist":
            resolved_artist = cand_data
        elif cand_type == "artist_legacy":
            try:
                artists = await self.db.get_all_artists(search_query=cand_data)
                if artists:
                    target_lc = cand_data.lower()
                    exact = next(
                        (a for a in artists if a["name"].lower() == target_lc),
                        None,
                    )
                    resolved_artist = (exact or artists[0])["name"]
            except Exception as exc:
                logger.debug("_apply_anaphora_resolution: legacy artist resolve failed: %s", exc)

        if resolved_track is None and resolved_artist is None:
            return None

        # Rewrite the intent in-place.
        if resolved_track:
            resolved_title = (resolved_track.get("title")
                              or resolved_track.get("track_title") or "")
            resolved_art = (resolved_track.get("artist")
                            or resolved_track.get("artist_name") or "")
            target_intent.query = f"{resolved_title} {resolved_art}".strip()
            target_intent.extras["resolved_track"] = resolved_track
            if needs_context and not self.engine.current_path:
                seed_path = resolved_track.get("path") or ""
                if seed_path:
                    target_intent.extras["seed_path_override"] = seed_path
                    target_intent.extras["seed_artist_override"] = resolved_art
            return (f"{resolved_title} by {resolved_art}"
                    if resolved_art else resolved_title or "that track")

        # resolved_artist path
        target_intent.query = resolved_artist
        if needs_context and not self.engine.current_path:
            try:
                conn = await self.db.get_connection()
                async with conn.execute(
                    """
                    SELECT t.path, t.title, ar.name AS artist
                    FROM tracks t
                    JOIN albums al ON al.id = t.album_id
                    JOIN artists ar ON ar.id = al.artist_id
                    WHERE ar.name = ? COLLATE NOCASE
                    LIMIT 1
                    """,
                    (resolved_artist,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    row = dict(row)
                    target_intent.extras["seed_path_override"] = row["path"]
                    target_intent.extras["seed_artist_override"] = resolved_artist
            except Exception as exc:
                logger.debug("_apply_anaphora_resolution: artist seed lookup failed: %s", exc)
        return resolved_artist



    # ── Handlers ────────────────────────────────────────────────────────────

    async def _handle_play_now(self, intent: ai.Intent) -> AssistantResponse:
        # Fetch a broad set of candidates. A specific title query usually
        # returns 1; an artist query like 'radiohead' returns many. We treat
        # the two cases differently: single match → play it, multiple →
        # enqueue the whole set with a randomised starting point so the user
        # gets variety without us toggling their shuffle setting.
        resolved = intent.extras.get("resolved_track")
        if resolved:
            hits = [resolved]
        else:
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

        # Disambiguation check: multiple hits with matching title across 2+ distinct artists
        q_clean = (intent.query or "").strip().lower()
        if len(hits) > 1 and q_clean:
            title_matches = [
                h for h in hits
                if q_clean in (h.get("title") or h.get("track_title") or "").lower()
            ]
            distinct_artists = {
                (h.get("artist") or h.get("artist_name") or "").lower()
                for h in title_matches
            }
            if len(title_matches) >= 2 and len(distinct_artists) >= 2:
                opts = [
                    ChoiceOption(
                        id=str(i + 1),
                        title=f"{h.get('title')} — {h.get('artist')}",
                        subtitle=f"Album: {h.get('album', 'Unknown Album')}",
                        payload=h,
                    )
                    for i, h in enumerate(title_matches[:4])
                ]

                async def _on_select(choice_opt: ChoiceOption) -> AssistantResponse:
                    t = choice_opt.payload
                    etrack = _to_engine_track(t)
                    self.engine.set_queue([etrack], start_index=0)
                    self._remember(etrack["path"])
                    return AssistantResponse(
                        spoken=f"{self._say('affirmative')} Playing '{t.get('title')}' by {t.get('artist')}.",
                        displayed=f"Playing choice: **{t.get('title')}** — {t.get('artist')}",
                        extras={"track": t},
                        deferred_play=True,
                    )

                dis_spoken = self._say("disambiguation", count=len(opts), query=intent.query)
                self.queue_choice(PendingChoice(
                    prompt=dis_spoken,
                    options=opts,
                    on_select_callback=_on_select,
                ))

                return AssistantResponse(
                    spoken=dis_spoken,
                    displayed=f"Multiple matches found for **{intent.query}**. Please choose one:",
                    extras={"options": [{"id": o.id, "title": o.title, "subtitle": o.subtitle} for o in opts]},
                )

        if len(engine_tracks) == 1:
            self.engine.set_queue(engine_tracks, start_index=0)
            self._remember(engine_tracks[0]["path"])
            album = first.get("album") or first.get("album_title") or ""
            duration = first.get("duration") or 0.0
            album_str = f" from the album '{album}'" if album else ""
            duration_str = ""
            if duration > 0:
                m, s = divmod(int(duration), 60)
                duration_str = f" ({m}m {s}s)" if m > 0 else f" ({s}s)"
            return AssistantResponse(
                spoken=f"{self._say('affirmative')} {prefix_spoken}playing '{first.get('title')}' by {first.get('artist')}{album_str}{duration_str}.",
                displayed=f"Certainly. {prefix_displayed}**{first.get('title')}** — {first.get('artist')}{album_str}{duration_str}",
                extras={"track": first},
                deferred_play=True,
            )

        # Multi-match: start from a random index so repeated 'play radiohead'
        # doesn't open with the same track every time.
        start = random.randint(0, len(engine_tracks) - 1)
        self.engine.set_queue(engine_tracks, start_index=start)
        self._remember(engine_tracks[start]["path"])
        first = hits[start]
        album = first.get("album") or first.get("album_title") or ""
        duration = first.get("duration") or 0.0
        album_str = f" from the album '{album}'" if album else ""
        duration_str = ""
        if duration > 0:
            m, s = divmod(int(duration), 60)
            duration_str = f" ({m}m {s}s)" if m > 0 else f" ({s}s)"
        return AssistantResponse(
            spoken=(
                f"{self._say('affirmative')} {prefix_spoken}{len(engine_tracks)} tracks matching "
                f"'{intent.query}'. Starting with '{first.get('title')}' by {first.get('artist')}{album_str}{duration_str}."
            ),
            displayed=(
                f"Queued **{len(engine_tracks)}** matches for **{intent.query}**. "
                f"{prefix_displayed}**{first.get('title')}** — {first.get('artist')}{album_str}{duration_str}."
            ),
            # is_multi flags the random opener as a non-canonical track seed.
            # The artist is still meaningful for 'more by them' follow-ups.
            extras={"queued": len(engine_tracks), "first": first,
                    "is_multi": True, "query": intent.query or ""},
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
            # entity_intent flags this 'track' as a non-canonical seed so the
            # persistence layer doesn't anchor future 'play it' on a random
            # song the user never asked for.
            extras={"track": first, "queued": len(engine_tracks),
                    "entity_intent": "play_random"},
            deferred_play=True,
        )

    async def _handle_queue_add(self, intent: ai.Intent) -> AssistantResponse:
        track = intent.extras.get("resolved_track") or await self._resolve_query(intent.query or "")
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

        album = track.get("album") or track.get("album_title") or ""
        duration = track.get("duration") or 0.0
        album_str = f" from the album '{album}'" if album else ""
        duration_str = ""
        if duration > 0:
            m, s = divmod(int(duration), 60)
            duration_str = f" ({m}m {s}s)" if m > 0 else f" ({s}s)"

        q_len = len(self.engine.queue)
        q_suffix_spoken = f" The queue now has {q_len} tracks." if q_len > 1 else ""
        q_suffix_displayed = f" [Queue size: {q_len}]"

        return AssistantResponse(
            spoken=f"{self._say('affirmative')} {verb}: '{track.get('title')}' by {track.get('artist')}{album_str}{duration_str}.{q_suffix_spoken}",
            displayed=f"{verb}: **{track.get('title')}** — {track.get('artist')}{album_str}{duration_str}.{q_suffix_displayed}",
            extras={"track": track},
        )

    async def _handle_queue_next(self, intent: ai.Intent) -> AssistantResponse:
        track = intent.extras.get("resolved_track") or await self._resolve_query(intent.query or "")
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

        album = track.get("album") or track.get("album_title") or ""
        duration = track.get("duration") or 0.0
        album_str = f" from the album '{album}'" if album else ""
        duration_str = ""
        if duration > 0:
            m, s = divmod(int(duration), 60)
            duration_str = f" ({m}m {s}s)" if m > 0 else f" ({s}s)"

        q_len = len(self.engine.queue)
        q_suffix_spoken = f" The queue now has {q_len} tracks." if q_len > 1 else ""
        q_suffix_displayed = f" [Queue size: {q_len}]"

        return AssistantResponse(
            spoken=f"{self._say('affirmative')} {verb}: '{track.get('title')}' by {track.get('artist')}{album_str}{duration_str}.{q_suffix_spoken}",
            displayed=f"{verb}: **{track.get('title')}** — {track.get('artist')}{album_str}{duration_str}.{q_suffix_displayed}",
            extras={"track": track},
        )

    async def _handle_play_similar(self, intent: ai.Intent) -> AssistantResponse:
        try:
            missing = await self.db.get_tracks_missing_features(track_graph.FEATURES_VERSION)
            if len(missing) > 0:
                return AssistantResponse(
                    spoken=f"I notice {len(missing)} tracks are not analyzed yet, sir. Please complete the DSP analysis before initiating a similarity walk.",
                    displayed=f"Play Similar is unavailable. {len(missing)} tracks lack DSP features. Run the Jarvis analyzer first.",
                    success=False,
                )
        except Exception as exc:
            logger.warning("Failed to verify missing features in assistant: %s", exc)

        seed_path = intent.extras.get("seed_path_override") or self.engine.current_path
        if not seed_path:
            return AssistantResponse(
                spoken="Nothing is playing right now.",
                displayed="No current track — start something first, then ask for similar tracks.",
                success=False,
            )
 
        avoid = await self._avoid_set()
        avoid.add(seed_path)
        
        # Save the original seed track on the engine for subsequent continuation walks
        self.engine.play_similar_seed_path = seed_path
 
        # Seed-anchored smooth walk over the acoustic graph. The 0.3·seed term
        # keeps it anchored, and the metadata/cluster factors keep it in the
        # seed's genre/community; dead-end steps fall back to seed neighbours.
        try:
            from utils.streamrip_api import get_walk_params
            temp, mmr = get_walk_params()
            walk_paths = await track_graph.walk(
                self.db, seed_path,
                length=12,
                avoid=avoid,
                mmr_lambda=mmr,   # suppress remix / alt-mix chaining
                temperature=temp,   # vary the queue across repeat requests
            )
        except Exception as exc:
            logger.warning("track_graph.walk failed: %s", exc)
            walk_paths = []

        if not walk_paths:
            return AssistantResponse(
                spoken="I don't have enough information to find similar tracks yet.",
                displayed=(
                    "The music graph hasn't been built for this track yet. "
                    "Open Settings → Permissions to enable file access, then "
                    "re-run the assistant initialisation."
                ),
                success=False,
            )

        added = 0
        engine_tracks = []
        for p in walk_paths:
            row = await self.db.get_track_full(p)
            if not row:
                continue
            engine_tracks.append(_to_engine_track(row))

        verb = intent.extras.get("verb")
        is_queue = verb and verb.lower().strip() in ("add", "queue", "enqueue", "put")

        first_row = await self.db.get_track_full(walk_paths[0])
        first_name = (
            f"{first_row.get('title')} — {first_row.get('artist')}"
            if first_row else "a similar track"
        )

        seed_row = await self.db.get_track_full(seed_path)
        seed_name = f"'{seed_row.get('title')}' by {seed_row.get('artist')}" if seed_row else "the active track"

        if is_queue and self.engine.queue:
            for t in engine_tracks:
                self.engine.queue_last(t)
                self._remember(t["path"], seed_path=seed_path)
                added += 1
            return AssistantResponse(
                spoken=f"I've added {added} tracks similar to {seed_name} to the queue.",
                displayed=(
                    f"Similarity sequence initiated based on **{seed_name}**. Queued **{added}** tracks "
                    f"(via acoustic walk). Next similar: **{first_name}**."
                ),
                extras={"added": added, "kind": "walk",
                        "entity_intent": "play_similar_bulk"},
            )
        else:
            self.engine.set_queue(engine_tracks, start_index=0)
            for t in engine_tracks:
                self._remember(t["path"], seed_path=seed_path)
                added += 1
            return AssistantResponse(
                spoken=f"{self._say('discovery')} Playing tracks similar to {seed_name}. Starting with {first_name}.",
                displayed=(
                    f"Similarity sequence initiated based on **{seed_name}**. Now playing **{added}** tracks "
                    f"(via acoustic walk). First similar: **{first_name}**."
                ),
                extras={"added": added, "kind": "walk",
                        "entity_intent": "play_similar_bulk"},
                deferred_play=True,
            )

    async def _handle_play_more_by(self, intent: ai.Intent) -> AssistantResponse:
        seed_path = intent.extras.get("seed_path_override") or self.engine.current_path
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
        avoid = await self._avoid_set()
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

        artist_name = intent.extras.get('seed_artist_override') or self.engine.current_artist
        if not artist_name:
            seed_row = await self.db.get_track_full(seed_path)
            if seed_row:
                artist_name = seed_row.get("artist")
        artist_name = artist_name or "this artist"

        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Queued {added} more tracks by {artist_name}.",
            displayed=f"Queued **{added}** more tracks by **{artist_name}**.",
            extras={"added": added},
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
        self.engine.clear_queue()
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

    async def _handle_help(self, _intent: ai.Intent) -> AssistantResponse:
        spoken_msg = (
            "I can manage your playback, curate playlists, edit the active queue, "
            "steer the music mood, or look up track trivia, sir. Ask me 'what can you do' "
            "or type 'help' anytime."
        )
        displayed_msg = (
            "### Jarvis Protocol & Capabilities\n\n"
            "**Playback & Search**\n"
            "• `play [song/artist/album]` — Search & play immediately (asks if multiple matches)\n"
            "• `1`, `2`, or `option [N]` — Select a choice during multi-match prompts\n"
            "• `pause` | `resume` | `skip` | `previous` | `shuffle` | `mute` | `unmute` | `stop`\n\n"
            "**Queue & Playlist Curation**\n"
            "• `add [song] to queue` | `play [song] next` — Append or insert tracks\n"
            "• `remove track [N]` | `remove [song]` — Drop item from active queue\n"
            "• `move [song/N] to top` — Reorder queue tracks\n"
            "• `save queue as playlist [Name]` — Export current queue to a local playlist\n\n"
            "**Discovery & Mood Steering**\n"
            "• `play similar` | `more like this` — Walk acoustic similarity graph\n"
            "• `play more by this artist` | `play the usual` | `surprise me`\n"
            "• `play something chill` | `make queue energetic` — Mood/Vibe steering\n\n"
            "**Context & Utility**\n"
            "• `tell me about this track` | `track info` — Metadata & acoustic details\n"
            "• `who made you` | `system status` | `what time is it` | `rescan dsp`\n"
            "• Anaphora memory: `play their top songs`, `queue that album`"
        )
        return AssistantResponse(spoken=spoken_msg, displayed=displayed_msg)


    async def _handle_greet(self, _intent: ai.Intent) -> AssistantResponse:
        try:
            tracks = await self.db.get_most_played(limit=20)
        except Exception:
            tracks = []

        if tracks and random.random() < 0.5:
            async def play_the_usual() -> AssistantResponse:
                track = random.choice(tracks)
                engine_track = _to_engine_track(track)
                self.engine.set_queue([engine_track], start_index=0)
                self._remember(engine_track["path"])
                
                title = track.get("title") or "Unknown Song"
                artist = track.get("artist") or "Unknown Artist"
                return AssistantResponse(
                    spoken=f"Very good, sir. Queuing up {title} by {artist}, one of your favorites.",
                    displayed=f"Playing the usual: **{title}** — {artist}",
                    deferred_play=True,
                )

            self.queue_confirmation(
                PendingConfirmation(
                    prompt="Good to see you, sir. Shall I queue up the usual?",
                    on_yes_callback=play_the_usual,
                    on_no_msg="Understood. Let me know if you need anything, sir.",
                )
            )
            return AssistantResponse(
                spoken="Good to see you, sir. Shall I queue up the usual?",
                displayed="Good to see you, sir. Shall I queue up the usual?",
            )

        phrase = self._say("greeting")
        return AssistantResponse(
            spoken=phrase,
            displayed=phrase,
        )

    async def _handle_play_the_usual(self, _intent: ai.Intent) -> AssistantResponse:
        try:
            tracks = await self.db.get_most_played(limit=20)
        except Exception:
            tracks = []

        if not tracks:
            return AssistantResponse(
                spoken="I don't have enough play history to know what your usual is, sir.",
                displayed="No play history found — play count data is empty.",
                success=False,
            )

        track = random.choice(tracks)
        engine_track = _to_engine_track(track)
        self.engine.set_queue([engine_track], start_index=0)
        self._remember(engine_track["path"])

        title = track.get("title") or "Unknown Song"
        artist = track.get("artist") or "Unknown Artist"
        album = track.get("album") or track.get("album_title") or ""
        duration = track.get("duration") or 0.0
        album_str = f" from the album '{album}'" if album else ""
        duration_str = ""
        if duration > 0:
            m, s = divmod(int(duration), 60)
            duration_str = f" ({m}m {s}s)" if m > 0 else f" ({s}s)"

        return AssistantResponse(
            spoken=f"Very good, sir. Queuing up {title} by {artist}{album_str}{duration_str}, one of your favorites.",
            displayed=f"Playing the usual: **{title}** — {artist}{album_str}{duration_str}",
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

    # ── Conversational Handlers ─────────────────────────────────────────────

    async def _handle_creator(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="I am Jarvis, an assistant configured for library and audio playback management.",
            displayed="Jarvis Audio Assistant — Configured for local library management."
        )

    async def _handle_coffee(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="I can only assist with audio playback and library management, sir.",
            displayed="Functionality unavailable. Jarvis is limited to audio playback and library operations."
        )

    async def _handle_hal(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="Systems are operating normally, sir. What would you like to play?",
            displayed="Systems operational. Ready for your playback command."
        )

    async def _handle_iron_man(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="Acknowledged, sir. Systems are online and ready for your command.",
            displayed="Systems online. Ready for playback directives."
        )

    async def _handle_joke(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="I am better equipped for music curation than comedy, sir, but I am happy to manage your queue.",
            displayed="I am better equipped for music curation than comedy, sir, but I am happy to manage your queue."
        )

    async def _handle_time_date(self, _intent: ai.Intent) -> AssistantResponse:
        import datetime
        now = datetime.datetime.now()
        time_str = now.strftime("%I:%M %p").lstrip("0")
        date_str = now.strftime("%A, %B %d, %Y")
        return AssistantResponse(
            spoken=f"It is currently {time_str} on {date_str}, sir.",
            displayed=f"Time: **{time_str}** | Date: **{date_str}**"
        )

    async def _handle_status(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="All systems nominal, sir. Audio decoders operating at peak performance and music graph matrix is standing by.",
            displayed="**System Diagnostics**\n• Core Engine: Operational\n• Audio Service: Active\n• Database Index: Synced"
        )

    async def _handle_thanks(self, _intent: ai.Intent) -> AssistantResponse:
        return AssistantResponse(
            spoken="You are very welcome, sir.",
            displayed="You are very welcome, sir."
        )

    async def _handle_quote(self, _intent: ai.Intent) -> AssistantResponse:
        quotes = [
            ('"Where words fail, music speaks." — Hans Christian Andersen',
             '*"Where words fail, music speaks."*\n— **Hans Christian Andersen**'),
            ('"Music is the shorthand of emotion." — Leo Tolstoy',
             '*"Music is the shorthand of emotion."*\n— **Leo Tolstoy**'),
            ('"Without music, life would be a mistake." — Friedrich Nietzsche',
             '*"Without music, life would be a mistake."*\n— **Friedrich Nietzsche**')
        ]
        spoken, displayed = random.choice(quotes)
        return AssistantResponse(spoken=spoken, displayed=displayed)

    # ── Advanced Conversational Queue & Trivia Handlers ──────────────────────

    async def _handle_queue_remove(self, intent: ai.Intent) -> AssistantResponse:
        if not self.engine.queue:
            return AssistantResponse(
                spoken="The queue is currently empty, sir.",
                displayed="Queue is empty.",
                success=False,
            )
        q = (intent.query or "").strip().lower()
        removed_track = None

        if q.isdigit():
            idx = int(q) - 1
            if 0 <= idx < len(self.engine.queue):
                removed_track = self.engine.queue.pop(idx)

        if not removed_track:
            if q in ("last", "the last song", "the last track", "last track"):
                removed_track = self.engine.queue.pop(-1)
            elif q in ("first", "the first song", "the first track"):
                removed_track = self.engine.queue.pop(0)
            else:
                for i, tr in enumerate(self.engine.queue):
                    t_title = (tr.get("track_title") or tr.get("title") or "").lower()
                    if q in t_title:
                        removed_track = self.engine.queue.pop(i)
                        break

        if not removed_track:
            return AssistantResponse(
                spoken=f"I couldn't find '{intent.query}' in the current queue, sir.",
                displayed=f"Item **{intent.query}** not found in active queue.",
                success=False,
            )

        title = removed_track.get("track_title") or removed_track.get("title") or "Track"
        artist = removed_track.get("artist_name") or removed_track.get("artist") or "Unknown Artist"
        remaining = len(self.engine.queue)
        return AssistantResponse(
            spoken=self._say("queue_remove", title=title, artist=artist, remaining=remaining, affirmative=self._say("affirmative")),
            displayed=f"Removed: **{title}** — {artist}. Queue remaining: **{remaining}**.",
        )

    async def _handle_queue_move(self, intent: ai.Intent) -> AssistantResponse:
        if not self.engine.queue or len(self.engine.queue) < 2:
            return AssistantResponse(
                spoken="The queue does not have enough tracks to reorder, sir.",
                displayed="Not enough tracks in queue to reorder.",
                success=False,
            )
        q = (intent.query or "").strip().lower()
        target_idx = None
        if q.isdigit():
            idx = int(q) - 1
            if 0 <= idx < len(self.engine.queue):
                target_idx = idx

        if target_idx is None:
            for i, tr in enumerate(self.engine.queue):
                t_title = (tr.get("track_title") or tr.get("title") or "").lower()
                if q in t_title:
                    target_idx = i
                    break

        if target_idx is None:
            return AssistantResponse(
                spoken=f"I couldn't find '{intent.query}' in the active queue, sir.",
                displayed=f"Item **{intent.query}** not found in queue.",
                success=False,
            )

        track = self.engine.queue.pop(target_idx)
        curr_idx = getattr(self.engine, "current_index", 0)
        insert_at = min(curr_idx + 1, len(self.engine.queue))
        self.engine.queue.insert(insert_at, track)

        title = track.get("track_title") or track.get("title") or "Track"
        return AssistantResponse(
            spoken=self._say("queue_move", title=title, pos=insert_at + 1, affirmative=self._say("affirmative")),
            displayed=f"Moved **{title}** to position #{insert_at + 1} in queue.",
        )

    async def _handle_save_queue(self, intent: ai.Intent) -> AssistantResponse:
        if not self.engine.queue:
            return AssistantResponse(
                spoken="There are no active walk tracks in your queue, sir.",
                displayed="Queue is empty. Cannot save walk.",
                success=False,
            )
        name = (intent.query or "Saved Walk").strip()
        try:
            playlist_id = await self.db.create_playlist(name)
        except Exception:
            import time
            name = f"{name} ({int(time.time())})"
            playlist_id = await self.db.create_playlist(name)

        # Save the current walk: tracks from the current playing position onwards
        curr_idx = getattr(self.engine, "current_index", 0)
        if curr_idx < 0 or curr_idx >= len(self.engine.queue):
            curr_idx = 0
        walk_tracks = self.engine.queue[curr_idx:]

        added_count = 0
        for track in walk_tracks:
            path = track.get("path")
            if path:
                await self.db.add_track_to_playlist(playlist_id, path)
                added_count += 1

        return AssistantResponse(
            spoken=self._say("queue_save", name=name, count=added_count, affirmative=self._say("affirmative")),
            displayed=f"Saved current walk to playlist: **{name}** ({added_count} tracks).",
        )

    async def _handle_mood_steer(self, intent: ai.Intent) -> AssistantResponse:
        mood = (intent.query or "chill").lower()
        tracks = await self.db.get_all_tracks()
        if not tracks:
            return AssistantResponse(
                spoken="Your library is currently empty, sir.",
                displayed="Library empty.",
                success=False,
            )

        def mood_score(t: dict) -> float:
            score = 0.0
            tempo = float(t.get("bpm") or t.get("tempo") or 0.0)
            genre = (t.get("genre") or "").lower()
            title = (t.get("title") or t.get("track_title") or "").lower()
            album = (t.get("album") or t.get("album_title") or "").lower()
            text = f"{genre} {title} {album}"

            if mood in ("chill", "relaxing", "calm", "quiet", "slow", "mellow"):
                if tempo > 0:
                    score += max(0.0, (120.0 - tempo) / 10.0)
                chill_keywords = ("chill", "lo-fi", "lofi", "ambient", "acoustic", "piano", "jazz", "downtempo", "sleep", "calm", "relax")
                for kw in chill_keywords:
                    if kw in text:
                        score += 5.0
            elif mood in ("energetic", "upbeat", "fast", "intense", "workout", "party", "hype"):
                if tempo > 0:
                    score += max(0.0, (tempo - 100.0) / 10.0)
                hype_keywords = ("rock", "metal", "dance", "electronic", "house", "techno", "pop", "hip hop", "rap", "punk", "workout", "energy")
                for kw in hype_keywords:
                    if kw in text:
                        score += 5.0
            elif mood in ("focus", "study", "work", "instrumental"):
                focus_keywords = ("ambient", "classical", "instrumental", "piano", "soundtrack", "study", "chill")
                for kw in focus_keywords:
                    if kw in text:
                        score += 5.0

            score += random.random() * 2.0
            return score

        sorted_tracks = sorted(tracks, key=mood_score, reverse=True)
        selected = sorted_tracks[:15]
        random.shuffle(selected)

        engine_tracks = [_to_engine_track(t) for t in selected]
        self.engine.set_queue(engine_tracks, start_index=0)
        self._remember(engine_tracks[0]["path"])

        first = selected[0]
        title = first.get("title") or first.get("track_title") or "Unknown"
        artist = first.get("artist") or first.get("artist_name") or "Unknown Artist"

        return AssistantResponse(
            spoken=self._say("mood_steer", mood=mood, title=title, artist=artist, affirmative=self._say("affirmative")),
            displayed=f"Curated **{mood.capitalize()} Mix** ({len(selected)} tracks). Playing: **{title}** — {artist}.",
            extras={"track": first, "mood": mood},
            deferred_play=True,
        )

    async def _handle_track_info(self, intent: ai.Intent) -> AssistantResponse:
        current = None
        if hasattr(self.engine, "current_track") and self.engine.current_track:
            current = self.engine.current_track
        elif self.engine.queue and hasattr(self.engine, "current_index"):
            idx = getattr(self.engine, "current_index", 0)
            if 0 <= idx < len(self.engine.queue):
                current = self.engine.queue[idx]

        if not current:
            return AssistantResponse(
                spoken="No track is currently playing, sir.",
                displayed="No active track playing.",
                success=False,
            )

        title = current.get("track_title") or current.get("title") or "Unknown Track"
        artist = current.get("artist_name") or current.get("artist") or "Unknown Artist"
        album = current.get("album_title") or current.get("album") or "Single / Unknown Album"
        duration = float(current.get("duration", 0.0) or 0.0)
        dur_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration > 0 else "Unknown length"

        spoken = self._say("track_info", title=title, artist=artist, album=album, duration=dur_str)
        displayed = f"**Track Info**\n• Title: **{title}**\n• Artist: **{artist}**\n• Album: **{album}**\n• Duration: **{dur_str}**"

        return AssistantResponse(
            spoken=spoken,
            displayed=displayed,
            extras={"track": current},
        )

    # ── Dispatch table ──────────────────────────────────────────────────────
    # Filled below the class so the method references resolve.


AssistantRunner._INTENT_DISPATCH = {
    ai.INTENT_PLAY_NOW:        AssistantRunner._handle_play_now,
    ai.INTENT_QUEUE_ADD:       AssistantRunner._handle_queue_add,
    ai.INTENT_QUEUE_NEXT:      AssistantRunner._handle_queue_next,
    ai.INTENT_CHOICE_SELECT:   AssistantRunner._handle_unknown,
    ai.INTENT_QUEUE_REMOVE:    AssistantRunner._handle_queue_remove,
    ai.INTENT_QUEUE_MOVE:      AssistantRunner._handle_queue_move,
    ai.INTENT_SAVE_QUEUE:      AssistantRunner._handle_save_queue,
    ai.INTENT_MOOD_STEER:      AssistantRunner._handle_mood_steer,
    ai.INTENT_TRACK_INFO:      AssistantRunner._handle_track_info,
    ai.INTENT_PLAY_SIMILAR:    AssistantRunner._handle_play_similar,
    ai.INTENT_PLAY_MORE_BY:    AssistantRunner._handle_play_more_by,
    ai.INTENT_PLAY_RANDOM:     AssistantRunner._handle_play_random,
    ai.INTENT_SKIP:            AssistantRunner._handle_skip,
    ai.INTENT_PREV:            AssistantRunner._handle_prev,
    ai.INTENT_PAUSE:           AssistantRunner._handle_pause,
    ai.INTENT_RESUME:          AssistantRunner._handle_resume,
    ai.INTENT_STOP:            AssistantRunner._handle_stop,
    ai.INTENT_CLEAR_QUEUE:     AssistantRunner._handle_clear_queue,
    ai.INTENT_SHUFFLE:         AssistantRunner._handle_shuffle,
    ai.INTENT_MUTE:            AssistantRunner._handle_mute,
    ai.INTENT_UNMUTE:          AssistantRunner._handle_unmute,
    ai.INTENT_PLAY_THE_USUAL:   AssistantRunner._handle_play_the_usual,
    ai.INTENT_GREET:             AssistantRunner._handle_greet,
    ai.INTENT_HELP:            AssistantRunner._handle_help,
    ai.INTENT_CREATOR:         AssistantRunner._handle_creator,
    ai.INTENT_COFFEE:          AssistantRunner._handle_coffee,
    ai.INTENT_HAL:             AssistantRunner._handle_hal,
    ai.INTENT_IRON_MAN:        AssistantRunner._handle_iron_man,
    ai.INTENT_JOKE:            AssistantRunner._handle_joke,
    ai.INTENT_TIME_DATE:       AssistantRunner._handle_time_date,
    ai.INTENT_STATUS:          AssistantRunner._handle_status,
    ai.INTENT_THANKS:          AssistantRunner._handle_thanks,
    ai.INTENT_QUOTE:           AssistantRunner._handle_quote,
    ai.INTENT_UNKNOWN:         AssistantRunner._handle_unknown,
}
