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
from typing import Optional, List, Callable

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
class PendingPlaylistCreation:
    """Conversational state for the two-step empty playlist wizard."""
    name: Optional[str] = None


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
        # Conversational playlist flow wizard state
        self._playlist_flow: Optional[PendingPlaylistCreation] = None
        self._history_cache: Optional[dict] = None
        # Optional injected callable returning the live in-memory history
        # list. When set (by AssistantView), the resolver skips the disk
        # round-trip through ChatMemoryManager and reads in-process state
        # directly. Reset per-dispatch by dispatch() / dispatch_text().
        self._history_provider: Optional[Callable[[], list]] = None

    def queue_confirmation(self, prompt: PendingConfirmation) -> None:
        """Stage a pending yes/no for the next user turn. Replaces any
        previously-pending confirmation; the assistant only ever holds one
        open question at a time."""
        self._pending = prompt

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
        "dsp_prompt": [
            "Good day, sir. I notice **{missing}** of your **{total}** tracks are not DSP-analyzed. Without features, similarity walks won't work. Analyze now? (yes/no)",
            "Systems report **{missing}** of **{total}** tracks lack DSP features. This affects similarity discovery. Shall I proceed with background analysis, sir?",
            "Sir, there are **{missing}** unindexed tracks in your database of **{total}**. I recommend running the DSP analyzer. Initiate scan now? (yes/no)",
            "Greetings, sir. Detected **{missing}** tracks requiring DSP profiling. Run analyzer to optimize graph? (yes/no)",
            "Acoustic graph scan complete, sir. **{missing}** of **{total}** tracks lack DSP parameters. Launch background analysis now? (yes/no)"
        ],
        "dsp_prompt_speak": [
            "I notice {missing} of your tracks are not analyzed, sir. Shall I run the analyzer now?",
            "Greetings, sir. Detected {missing} tracks lacking DSP profiles. Analyze them now?",
            "Sir, {missing} tracks lack acoustic features. Initiate the database scan?",
            "There are {missing} unprofiled tracks, sir. Run background acoustic analysis?",
            "I've found {missing} tracks without acoustic metrics, sir. Run the analyzer?"
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
        # Check intent types and set jarvis_controlled state on the audio engine accordingly
        play_or_queue_intents = {
            ai.INTENT_PLAY_NOW,
            ai.INTENT_QUEUE_ADD,
            ai.INTENT_QUEUE_NEXT,
            ai.INTENT_PLAY_SIMILAR,
            ai.INTENT_PLAY_MORE_BY,
            ai.INTENT_PLAY_RANDOM,
            ai.INTENT_PLAYLIST_PLAY,
        }
        stop_or_clear_intents = {
            ai.INTENT_STOP,
            ai.INTENT_CLEAR_QUEUE,
        }
        if intent.name in play_or_queue_intents:
            self.engine.jarvis_controlled = True
        elif intent.name in stop_or_clear_intents:
            self.engine.jarvis_controlled = False

        # 1. Intercept for active conversational playlist wizard
        if self._playlist_flow is not None:
            # Emergency playback controls override the conversational wizard
            EMERGENCY_COMMANDS = (
                ai.INTENT_SKIP, ai.INTENT_PREV, ai.INTENT_PAUSE, 
                ai.INTENT_RESUME, ai.INTENT_STOP, ai.INTENT_MUTE, 
                ai.INTENT_UNMUTE, ai.INTENT_SHUFFLE
            )
            if intent.name in EMERGENCY_COMMANDS:
                self._playlist_flow = None
                # Fall through to normal handler dispatch so playback controls execute instantly
            else:
                raw_text = (intent.raw or "").strip().lower()
                if raw_text in ("cancel", "abort", "stop", "nevermind", "forget it", "no"):
                    self._playlist_flow = None
                    return AssistantResponse(
                        spoken="Understood. Playlist creation canceled.",
                        displayed="Playlist creation canceled.",
                    )
                # Process the turn within the slot-filling flow
                return await self._handle_playlist_flow_step(intent)

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
            handler = self._INTENT_DISPATCH.get(intent.name, AssistantRunner._handle_unknown)
            return await handler(self, intent)
        except Exception as exc:
            logger.exception("AssistantRunner: handler failed for %s", intent.name)
            return AssistantResponse(
                spoken=self._say("error"),
                displayed=f"Error: {exc}",
                success=False,
            )

    async def _handle_playlist_flow_step(self, intent: ai.Intent) -> AssistantResponse:
        flow = self._playlist_flow
        if not flow:
            return AssistantResponse(
                spoken="Error: playlist flow is not active.",
                displayed="Flow inactive.",
                success=False
            )

        raw = (intent.raw or "").strip()

        # Step 1: Get Name
        if flow.name is None:
            if not raw:
                return AssistantResponse(
                    spoken="What should we name the playlist, sir?",
                    displayed="Playlist name cannot be empty. Please specify a name:",
                )
            
            clean_name = raw
            if intent.name == ai.INTENT_NAME_ENTITY and intent.query:
                clean_name = intent.query
            else:
                for prefix in ("call it ", "name it ", "make it ", "called ", "name the playlist "):
                    if clean_name.lower().startswith(prefix):
                        clean_name = clean_name[len(prefix):].strip()
            
            clean_name = clean_name.strip().strip("\"'").strip()
            
            # Check duplicate names
            try:
                playlists = await self.db.get_all_playlists()
                if playlists and any(p["name"].lower() == clean_name.lower() for p in playlists):
                    return AssistantResponse(
                        spoken=f"It seems a playlist called '{clean_name}' already exists, sir. What other name should we use?",
                        displayed=f"Playlist **{clean_name}** already exists. Choose a different name:",
                    )
            except Exception:
                pass

            try:
                await self.db.create_playlist(flow.name)
                self._playlist_flow = None
                return AssistantResponse(
                    spoken=f"{self._say('affirmative')} I have created the playlist '{flow.name}' for you.",
                    displayed=f"Created playlist: **{flow.name}**",
                )
            except Exception as exc:
                self._playlist_flow = None
                return AssistantResponse(
                    spoken=f"I couldn't create that playlist, sir: {exc}",
                    displayed=f"Failed to create playlist: {exc}",
                    success=False,
                )

    async def dispatch_text(self, text: str,
                            history_provider: Optional[Callable[[], list]] = None,
                            ) -> AssistantResponse:
        """Convenience: parse + dispatch in one call."""
        intent = ai.parse(text)
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
        "playlist_play":  1.0,
        # passive / informational
        "search_artist":  0.5,
        "search_track":   0.5,
        "search_album":   0.5,
        "now_playing":    0.5,
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
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} {verb}: {track.get('title')} by {track.get('artist')}.",
            displayed=f"{verb}: **{track.get('title')}** — {track.get('artist')}",
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
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} {verb}: {track.get('title')} by {track.get('artist')}.",
            displayed=f"{verb}: **{track.get('title')}** — {track.get('artist')}",
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
            walk_paths = await track_graph.walk(
                self.db, seed_path,
                length=12,
                avoid=avoid,
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

        if is_queue and self.engine.queue:
            for t in engine_tracks:
                self.engine.queue_last(t)
                self._remember(t["path"], seed_path=seed_path)
                added += 1
            return AssistantResponse(
                spoken=f"I've added {added} similar tracks to the queue.",
                displayed=(
                    f"Similarity sequence initiated. Queued **{added}** tracks "
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
                spoken=f"{self._say('discovery')} Playing tracks similar to this. Starting with {first_name}.",
                displayed=(
                    f"Similarity sequence initiated. Now playing **{added}** tracks "
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
        return AssistantResponse(
            spoken=f"{self._say('affirmative')} Queued {added} more by this artist.",
            displayed=f"Queued **{added}** more tracks by {intent.extras.get('seed_artist_override') or self.engine.current_artist or 'this artist'}.",
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

    async def _handle_now_playing(self, _intent: ai.Intent) -> AssistantResponse:
        title = getattr(self.engine, "current_track", "") or ""
        artist = getattr(self.engine, "current_artist", "") or ""
        if not title:
            return AssistantResponse(spoken="Nothing is playing.", displayed="No current track.")
        # Build a minimal track dict so the persistence layer can stash a
        # structured entity instead of relying on bold-tag regex parsing.
        track_dict = {
            "path":   getattr(self.engine, "current_path", "") or "",
            "title":  title,
            "artist": artist,
            "album":  getattr(self.engine, "current_album", "") or "",
        }
        return AssistantResponse(
            spoken=self._say("status", track=title, artist=artist),
            displayed=f"**{title}** — {artist}",
            extras={"track": track_dict, "artist": artist},
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
            "I can manage your playback, queue tracks, create playlists, "
            "or walk the acoustic similarity graph, sir. Just say 'play more like this' "
            "or 'more by this artist' to begin."
        )
        displayed_msg = (
            "### Jarvis System Capabilities\n\n"
            "*   **Playback**: `play [song/artist]`, `pause`, `resume`, `skip`, `prev`, `shuffle`\n"
            "*   **Similarity Graph**: `play similar`, `more like this`, `more by this artist`\n"
            "*   **Playlists**: `create playlist [name]`, `add this to [playlist]`\n"
            "*   **Sub-systems**: `rescan dsp`, `clear queue`, `download [song]`"
        )
        return AssistantResponse(spoken=spoken_msg, displayed=displayed_msg)

    async def _handle_playlist_create(self, intent: ai.Intent) -> AssistantResponse:
        name = (intent.query or "").strip()
        if not name:
            self._playlist_flow = PendingPlaylistCreation()
            return AssistantResponse(
                spoken="What should we name the playlist, sir?",
                displayed="Playlist name cannot be empty. Please specify a name:",
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
    ai.INTENT_PLAYLIST_ADD:    AssistantRunner._handle_playlist_add,
    ai.INTENT_PLAYLIST_PLAY:   AssistantRunner._handle_playlist_play,
    ai.INTENT_GREET:           AssistantRunner._handle_greet,
    ai.INTENT_HELP:          AssistantRunner._handle_help,
    ai.INTENT_UNKNOWN:       AssistantRunner._handle_unknown,
}
