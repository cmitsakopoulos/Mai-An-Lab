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
    """Conversational state container to track progress during step-by-step
    playlist generation."""
    name: Optional[str] = None
    mood: Optional[str | bool] = None # None = unasked, False = empty playlist, str = mood name
    limit: Optional[int] = None       # None = unasked, int = track count


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
            "Understood, sir. Initiating requested sequence.",
            "Configuring parameters now. One moment.",
            "Compliance confirmed. Proceeding immediately, sir.",
            "Understood. Executing requested command.",
            "Task initialized. I am on it, sir.",
            "Command logged. Processing requested protocols.",
            "Acknowledged, sir. Routing requested instruction.",
            "Right away. Adjusting operational parameters.",
            "Confirming instruction. Executing now.",
            "Systems aligned, sir. Commencing operation.",
            "Indeed, sir. Commencing requested execution sequence.",
            "Very good, sir. I have dispatched the instruction to the main controller.",
            "Executing with absolute precision, sir.",
            "Your command is my directive, sir. Engaged.",
            "Making the necessary adjustments. One moment.",
            "Undertaking the task as we speak, sir.",
            "Signal routing updated. Proceeding with command parameters.",
            "Aligned and active. Initiating procedures, sir.",
            "Operational framework adjusted. Initiating your request.",
            "Command register locked. Commencing execution, sir.",
            "Processing your command immediately, sir.",
            "Your instruction has been prioritized and dispatched, sir.",
            "Task registered. Beginning background pipeline execution.",
            "Affirmative, sir. Aligning system states accordingly.",
            "Understood perfectly. Routing requests to the designated controllers.",
            "Consider it done. Initiating high-priority stream configuration.",
            "At once, sir. Executing command with full system authorization.",
            "Adjusting music engine registers to match your request.",
            "Acknowledged. Setting state transitions to active.",
            "Command received and verified. Commencing execution sequence, sir.",
            "Right away, sir. Updating local control registries.",
            "Always happy to oblige, sir. Initiating the procedure.",
            "Your directive is registered. Executing now.",
            "Indeed, sir. Directing the audio subsystem to execute.",
            "Understood. Applying requested adjustments to active deck.",
            "Command accepted, sir. The system is responding.",
            "Confirmed. Applying high-performance directives.",
            "Absolutely, sir. Commencing requested command pattern.",
            "Instructing database and playback controllers to align.",
            "Sequence initiated. Ready for next input when you are, sir."
        ],
        "searching": [
            "Scanning your library database...",
            "Accessing the music graph...",
            "Locating the requested tracks...",
            "Sifting through the archives...",
            "Triangulating metadata signatures...",
            "Database query in progress, sir.",
            "Running heuristic match algorithms across your library...",
            "Cross-referencing index nodes for matching waveforms...",
            "Analyzing local storage sectors, sir. Stand by.",
            "Searching indexing tables for matching artist and track nodes...",
            "Parsing musical catalogs for a matching entry, sir.",
            "Interrogating library directories for requested metadata...",
            "Executing deep index lookup, sir.",
            "Querying indexing engines. One moment...",
            "Scanning database indexes for matching records, sir.",
            "Filtering collection matrices for matches...",
            "Locating target nodes in the high-dimensional index structure...",
            "Correlating structural identifiers across your music directories...",
            "Interrogating local caches for the specified track record...",
            "Performing a full-spectrum sweep of the catalog, sir...",
            "Scanning active audio indexes. Please stand by, sir...",
            "Tracing references across the artist and album graphs...",
            "Heuristic database matching is currently in progress...",
            "Searching the local library database coordinates...",
            "Executing real-time query across active indices, sir.",
            "Traversing relational matrices to find a match...",
            "Scanning high-dimensional index matrices for target signatures...",
            "Querying local SQL indexes and cache regions...",
            "Scanning album and artist nodes for semantic matches...",
            "Searching high-fidelity indexing registers, sir...",
            "Parsing music databases for a matching identifier...",
            "Checking active stream cache and secondary indexes...",
            "Traversing the local music graph coordinates, sir...",
            "Running matching algorithms against track databases...",
            "Interrogating acoustic feature tables for matches...",
            "Searching index clusters for structural attributes, stand by...",
            "Traversing local repository tree for the target files...",
            "Analyzing spatial similarity matrices. Stand by, sir...",
            "Checking structural nodes in the artist-album linkage graph...",
            "Filtering relational tables with the specified parameters, sir...",
            "Iterating over database catalog files...",
            "Verifying track signature patterns across the indexing layer...",
            "Sweeping track catalogs and directories, sir...",
            "Executing deep search queries against indexing engines...",
            "Retrieving the requested record from cache matrices...",
            "Scanning music collection coordinates for a matching vector..."
        ],
        "error": [
            "I'm afraid I've encountered a system error, sir.",
            "My apologies, but that action was unsuccessful.",
            "It seems the system is unresponsive to that request.",
            "I've hit a bit of a snag in the audio sub-system, sir.",
            "Logic circuits seem to be reporting a conflict, sir.",
            "An exception has occurred in the internal pipeline, sir.",
            "Signal disruption detected. Unable to complete instruction.",
            "Warning: Audio controller returned an error code, sir.",
            "Operation aborted due to an internal execution error.",
            "Apologies, sir. Systems are reporting a command processing failure.",
            "Protocol collision detected. Please restate or retry, sir.",
            "I'm unable to resolve the requested instruction at this time.",
            "Operational conflict in the backend dispatcher, sir.",
            "Unable to execute. Hardware registers returned a fault.",
            "I've encountered an unexpected exception in the command dispatcher, sir.",
            "It seems the audio bridge has reported a critical registration fault.",
            "Apologies, sir. My instruction pipeline seems temporarily blocked.",
            "I'm detecting an anomaly in the local music database response.",
            "Hardware registers are busy or temporarily unresponsive, sir.",
            "I'm afraid I cannot complete that procedure at this moment.",
            "System diagnostics indicate an execution failure in the background tasks.",
            "The database returned an empty status record instead of a confirmation.",
            "Signal routing pipeline returned a status code error, sir.",
            "I have detected a data pipeline integrity fault during operations.",
            "My apologies, sir. An unexpected error has halted the request.",
            "It seems the audio bridge is temporarily unresponsive, sir.",
            "Logic pipeline encountered a validation error. Aborting sequence.",
            "Diagnostics report a collision in the command handler, sir.",
            "I've encountered an unexpected file access fault, sir.",
            "Operational failure in the music engine. Stand by for retry.",
            "An exception occurred while parsing operational parameters.",
            "Backend dispatcher returned a bad response. Retrying, sir...",
            "Apologies, sir. The database controller did not acknowledge the write.",
            "System exception detected in the playback state machine.",
            "Warning: Secondary indexing returned a read collision, sir.",
            "Unable to process. The system reported a hardware busy status.",
            "My command queue has experienced a synchronization anomaly, sir.",
            "Operational parameters returned an out-of-bounds error.",
            "I'm afraid the audio driver reported a state transition failure.",
            "Apologies, sir. The requested instruction triggered a null reference.",
            "Underlying audio service failed to initialize the stream.",
            "I've encountered a logic conflict while addressing the audio registers.",
            "Diagnostics report an unexpected callback timeout in the audio bridge.",
            "Command processing was interrupted by an internal system fault, sir."
        ],
        "not_found": [
            "I'm afraid I couldn't find a match for '{query}', sir.",
            "My apologies, but '{query}' is not in your local library.",
            "It seems '{query}' is missing from the database.",
            "Search protocols completed, but '{query}' remains elusive.",
            "I've searched every corner of the drive, but '{query}' isn't here.",
            "No matching index nodes found for '{query}', sir.",
            "Telemetry reports zero database hits for '{query}'.",
            "I've parsed the entire local catalog, but '{query}' is not registered.",
            "Heuristics returned no viable results matching '{query}', sir.",
            "Regrettably, sir, '{query}' does not appear in your collection.",
            "Database query yielded no positive matches for '{query}'.",
            "Search scope completed. '{query}' is currently unavailable, sir.",
            "Indices contain no trace of '{query}'.",
            "I could not locate '{query}' in either local indices or active caches.",
            "I've scanned the active library paths, but '{query}' returned null.",
            "Search protocols completed successfully, but '{query}' remains undiscovered.",
            "The index contains no reference matching '{query}', sir.",
            "I've traversed the entire catalog, but found no traces of '{query}'.",
            "It seems '{query}' is currently absent from your local directories.",
            "Query analysis failed to locate '{query}' in our local storage nodes.",
            "My heuristics found nothing resembling '{query}' in the music graph.",
            "No index references matched the search term '{query}', sir.",
            "I swept all local and external indexing tables for '{query}', with zero results.",
            "I've swept the database twice, sir, but '{query}' is not registered.",
            "I'm afraid '{query}' doesn't exist in our indexed music graph, sir.",
            "Telemetry reports no files matching '{query}' in our local storage.",
            "I have searched the index structure, but '{query}' has zero nodes.",
            "Apologies, sir. '{query}' returned no results in this library context.",
            "My search heuristics returned an empty result set for '{query}'.",
            "No active cache or indexing references matched '{query}', sir.",
            "I sweeps all directories, but '{query}' is not in the system.",
            "It appears '{query}' is not present in our current active collection.",
            "I've parsed all matching strings, but '{query}' is not in the DB.",
            "Unable to locate '{query}' across the library matrices.",
            "Search coordinates did not yield any positive hits for '{query}', sir.",
            "Indices show no record of '{query}'. Perhaps a scan is required?",
            "I could not find '{query}' in either metadata or file tables.",
            "Search pipeline returned null for '{query}', sir.",
            "No trace of '{query}' exists in our structural graph nodes.",
            "Query for '{query}' returned a total of zero matching files.",
            "My search indices have no registered entries for '{query}', sir.",
            "No matching artist, album, or track matches '{query}' in our database.",
            "I've combed through the entire catalog, but '{query}' seems missing."
        ],
        "unknown": [
            "I'm afraid I don't understand that command, sir.",
            "My apologies, sir, but that is not in my protocols.",
            "Could you rephrase that? I didn't quite catch the intent.",
            "I'm sorry, sir. My training does not cover that specific phrasing.",
            "I'm having trouble parsing that request, sir.",
            "Command unrecognized. I'm afraid that is outside my vocabulary matrix, sir.",
            "Syntax mismatch detected. Could you express your intent differently?",
            "Pardon me, sir, but I am unable to map that statement to an action.",
            "I didn't quite grasp that command, sir. Could you clarify?",
            "Semantic analysis yielded zero high-confidence matches, sir.",
            "That query seems to fall outside my operational directives.",
            "Command context unclear, sir. May I suggest rephrasing your request?",
            "Unrecognized phrasing. I stand ready for a different instruction, sir.",
            "I am fully functional, sir, but that request lies outside my current vocabulary.",
            "I didn't quite catch that, sir. Might I suggest a different command format?",
            "My natural language processor was unable to resolve that statement.",
            "Apologies, sir. That phrasing didn't map to any of my registered routines.",
            "I'm ready for your instructions, but I couldn't parse your last prompt.",
            "Could you speak a little clearer or use a different phrasing, sir?",
            "Input patterns are outside my current instructional matrix, sir.",
            "Parser error: Semantic intent remains highly ambiguous.",
            "Pardon me, sir, but that command is outside my syntax definitions.",
            "I didn't quite capture the intent behind that phrasing, sir.",
            "Semantic intent parsing returned an extremely low confidence score.",
            "Could you clarify that, sir? I couldn't map it to an action.",
            "Command unrecognized. My intent matrix had no matches, sir.",
            "I'm having trouble matching that request to my registered routines.",
            "Syntax error in the incoming instruction, sir. Could you rephrase?",
            "That falls outside my natural language instruction guidelines, sir.",
            "Apologies, sir. I'm afraid that phrasing is not in my database.",
            "I am ready for commands, but I didn't catch the action key in that.",
            "I couldn't identify the operational parameters in your request, sir.",
            "Semantic parsing failed. Might I suggest a simpler command structure?",
            "I'm afraid my processor couldn't resolve that prompt, sir.",
            "That query seems to exceed my standard instruction protocol, sir.",
            "Could you restate your instruction? I failed to parse the command.",
            "I'm at your disposal, sir, but I didn't recognize that instruction.",
            "Command context is ambiguous. Please try again with clear terms.",
            "I couldn't map that input to any active audio controller functions.",
            "That directive doesn't match any of my registered semantic flows, sir.",
            "Pardon me, sir, but I am unable to decode that specific command."
        ],
        "playback_control": [
            "Of course, sir. {action}.",
            "As you wish. {action}.",
            "Understood. {action}.",
            "Right away. {action}.",
            "Adjusting the output stream. {action}.",
            "Modifying active stream variables. {action}, sir.",
            "Executing playback adjustment. {action}.",
            "Audio driver updated. {action}, sir.",
            "Instruction successfully delivered to audio pipeline. {action}.",
            "Playback registers updated: {action}.",
            "Signal path modified. {action}, sir.",
            "Routing state transition. {action}.",
            "Modifying decibel and stream parameters. {action}.",
            "Synchronizing decks. {action}, sir.",
            "Instruction successfully registered. {action}.",
            "Active signal line updated. {action}, sir.",
            "Updating queue playback state. {action}.",
            "Audio output updated. {action}, sir.",
            "State updated to reflect: {action}.",
            "Instructing the audio service: {action}, sir.",
            "Modifying the playback registers: {action}.",
            "Audio engine updated: {action}, sir.",
            "Active queue state modified. {action}.",
            "Understood. Applying: {action}.",
            "Dispatched to music bridge: {action}, sir.",
            "Executing target transition: {action}.",
            "Updating the active deck: {action}, sir.",
            "Understood. Performing {action} immediately.",
            "Audio stream adjusted. {action}, sir.",
            "Playback parameters set: {action}.",
            "Directing playback controller to execute: {action}.",
            "Acknowledged. State changed: {action}, sir.",
            "Applying request to the active stream: {action}.",
            "Routing state update: {action}, sir.",
            "Deck parameters modified: {action}.",
            "Configuring the music engine: {action}, sir.",
            "Transitioning audio system: {action}.",
            "Instructing audio player to: {action}, sir.",
            "Confirmed. The audio player is performing: {action}.",
            "Playback system successfully updated to reflect: {action}."
        ],
        "discovery": [
            "Initiating similarity sequence, sir.",
            "Accessing the acoustic graph. One moment...",
            "Expanding the playback horizon, sir.",
            "Cross-referencing acoustic signatures...",
            "Heuristics suggest this might suit your mood, sir.",
            "Analyzing the sonic landscape. One moment...",
            "Calculating nearest acoustic neighbors in your library graph...",
            "Navigating acoustic edges for similar structural features...",
            "Comparing high-dimensional dsp feature representations, sir...",
            "Synthesizing an acoustic pathway matching the current vibe, sir.",
            "Locating tracks with adjacent sonic properties. Stands by...",
            "Traversing the music network to discover related tracks...",
            "Identifying acoustic matches matching the target signature, sir.",
            "Tracing the optimal path through similar acoustic dimensions...",
            "Calculating high-dimensional distances between structural features, sir...",
            "Compiling a list of highly correlated sonic properties...",
            "Generating a customized acoustic walk across the library graph...",
            "Cross-referencing similar mood coordinates in the audio space...",
            "Walking the acoustic edges. Fetching adjacent track records...",
            "Searching spatial embedding regions for sonic neighbors, sir.",
            "Analyzing spectral similarities to determine graph proximity...",
            "Tracing matching acoustic signatures across database clusters...",
            "Locating tracks with similar high-dimensional vectors, sir...",
            "Traversing our high-fidelity acoustic graph nodes...",
            "Executing high-dimensional distance queries in the music graph...",
            "Finding sonic neighbors matching this track's vibe, sir...",
            "Analyzing frequency and rhythm attributes for adjacent tracks...",
            "Scanning acoustic features to build an adjacent walk...",
            "Evaluating structural similarities across catalog coordinates...",
            "Checking the proximity matrices in your music graph, sir...",
            "Synthesizing a similar-vibed list from adjacent nodes...",
            "Navigating acoustic vectors for a harmonious transition, sir...",
            "Traversing graph edges to identify matching musical moods...",
            "Searching high-dimensional spatial indexes for sonic neighbors...",
            "Correlating acoustic signatures to expand the queue, sir...",
            "Walking adjacent music graph connections for matching vibes...",
            "Calculating feature distances to ensure a perfect queue fit...",
            "Retrieving sonically aligned tracks from the feature database...",
            "Parsing DSP feature representations for matching signatures, sir...",
            "Tracing acoustic edges to construct a seamless musical journey...",
            "Interrogating the music graph for similar acoustic properties, sir..."
        ],
        "status": [
            "Currently processing, sir.",
            "Systems are green. The track is {track}.",
            "This is {track} by {artist}, sir.",
            "Telemetry reports we are listening to {track}.",
            "Active deck playing: {track} by {artist}.",
            "We are currently streaming {track} by {artist}, sir.",
            "System status: Playing {track} from your local library.",
            "Signal output is active. Now playing: {track} by {artist}.",
            "The active signal path is currently streaming {track} by {artist}.",
            "Diagnostics report active deck playing: {track}.",
            "Current frequency output: {track} by {artist}, sir.",
            "We are currently outputting {track} by {artist} at maximum quality.",
            "Playback deck is operational. Playing {track}.",
            "We are listening to {track} by {artist}, sir.",
            "Active track is {track} from your local collection, sir.",
            "Active deck reporting: {track} by {artist}.",
            "Currently decoding: {track} by {artist}, sir.",
            "The stream is active with {track} by {artist}.",
            "Telemetry indicates playing: {track} by {artist}, sir.",
            "Output signal is active: {track} by {artist}.",
            "Current stream is {track} from your local storage.",
            "Active output deck is streaming {track} by {artist}, sir.",
            "Current playback status: {track} by {artist}.",
            "We are currently outputting {track} by {artist}.",
            "Active playback channel: {track} by {artist}, sir.",
            "Decoders are currently processing {track} by {artist}.",
            "The audio player is currently playing {track}.",
            "Stream coordinates point to {track} by {artist}, sir.",
            "Current output deck details: {track} by {artist}.",
            "Audio service is streaming {track} by {artist}.",
            "Active output registers show: {track} by {artist}, sir.",
            "System is playing {track} by {artist}.",
            "Now playing: {track} by {artist} from the library graph."
        ],
        "greeting": [
            "At your service, sir. I've mapped your library — what can I do for you?",
            "Systems online. Library graph fully indexed. How may I assist?",
            "Ready for your commands, sir. The music network is at your disposal.",
            "Good to see you, sir. I'm ready to manage your collection.",
            "All systems normal, sir. The audio matrix is online and listening.",
            "Jarvis interface operational. I am ready to queue your favorite selections, sir.",
            "Acoustic graph loaded, sir. What shall we listen to today?",
            "Standing by to direct the audio stream, sir. Awaiting your instruction.",
            "Ready to parse your musical desires, sir. Speak when ready.",
            "Library registers fully loaded and optimized, sir. How may I serve you?",
            "Always a pleasure to assist, sir. The music database is fully synced and at your service.",
            "Systems online. Library graph and mood attributes mapped. How shall we begin?",
            "Operational and standing by, sir. Let me know what you'd like to hear.",
            "Jarvis interface online. Audio matrix and high-dimensional graphs are fully initialized.",
            "Good day, sir. The music catalog is ready and fully optimized. Awaiting your directive.",
            "Database online, DSP parameters loaded. Standing ready for your instructions, sir.",
            "{time_greeting}, sir. The music network is at your disposal. What shall we play?",
            "{time_greeting}, sir. I've mapped your library. How may I assist you today?",
            "{time_greeting}, sir. Systems online and fully operational. Standing by for your instructions.",
            "{time_greeting}. Ready for your commands, sir. The acoustic graph is loaded.",
            "{time_greeting}, sir. Standing by to direct the audio stream. Awaiting your instruction.",
            "{time_greeting}, sir. Jarvis interface operational. The library matrix is fully synced.",
            "{time_greeting}, sir. Operational and standing by. Let me know what you'd like to hear.",
            "{time_greeting}, sir. Database online and DSP parameters loaded. Standing ready for your directive.",
            "{time_greeting}, sir. I've completed a full check of your library registers. What is your command?",
            "{time_greeting}, sir. All systems are green. Ready to curate the perfect vibe.",
            "{time_greeting}, sir. The music graph is fully operational. How shall we begin?",
            "{time_greeting}, sir. Ready to parse your requests and route your commands.",
            "{time_greeting}, sir. Ready to direct the stream. Tell me what is on your mind.",
            "{time_greeting}, sir. Decoders and index graphs are warmed up and waiting.",
            "{time_greeting}, sir. Standing ready. Let's traverse your musical landscape today.",
            "{time_greeting}, sir. I've indexed all matching artist and album nodes. Ready for instructions.",
            "{time_greeting}, sir. Interface active. How can I enhance your listening experience today?",
            "{time_greeting}, sir. Standing by. Command filters and semantic parser are fully operational.",
            "{time_greeting}, sir. Systems aligned and ready to execute your instruction queue.",
            "{time_greeting}, sir. The database caches are primed and indexed. Command me at your pleasure."
        ],
        "dsp_prompt": [
            "Good day, sir. I notice that **{missing}** of your **{total}** tracks haven't been DSP-analysed yet. Without features, they won't appear in mood searches or acoustic walks. Should I run the analyser now? Reply **yes** or **no**, or say 'rescan' later.",
            "Systems report **{missing}** out of **{total}** tracks lacking high-fidelity DSP features. This affects mood-aware discovery. Shall I proceed with the background analysis, sir?",
            "Welcome back, sir. There are **{missing}** unindexed tracks in your database of **{total}**. I highly recommend running the DSP graph builder. Would you like me to initiate the scan now?",
            "Greetings, sir. I have detected **{missing}** tracks that require DSP profiling. Running the analyser will optimize your acoustic graph. Shall we commence the analysis?",
            "Acoustic graph scanning complete, sir. **{missing}** out of **{total}** tracks lack DSP parameters. Should we launch the analysis process in the background now?"
        ],
        "dsp_prompt_speak": [
            "I notice {missing} of your tracks haven't been analysed yet, sir. Shall I run the analyser now?",
            "Greetings, sir. I have detected {missing} tracks lacking DSP profiles. Would you like me to analyze them now?",
            "Sir, {missing} tracks in your library are missing high-fidelity acoustic features. Should I initiate the database scan?",
            "There are {missing} unprofiled tracks, sir. Would you like me to run the background acoustic analysis?",
            "I've found {missing} tracks without acoustic metrics, sir. Would you like me to run the analyzer?"
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
            ai.INTENT_PLAY_MOOD,
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

            flow.name = clean_name

            # If mood is already known, skip step 2 and jump to count
            if flow.mood is not None:
                return AssistantResponse(
                    spoken=f"How many {flow.mood} songs should we populate '{flow.name}' with, sir? (Default is 20)",
                    displayed=f"How many **{flow.mood}** songs should we include? (Default: **20**)"
                )
            
            # Prompt for mood selection
            from utils import track_graph as tg
            return AssistantResponse(
                spoken="Should this be a smart playlist based on a mood, or a simple empty playlist, sir?",
                displayed=(
                    "Should this be a smart playlist based on a mood, or a simple empty playlist?\n\n"
                    "**Smart Moods**: " + ", ".join(sorted(tg.MOODS.keys())) + " (or type **empty**)"
                )
            )

        # Step 2: Get Mood
        if flow.mood is None:
            input_mood = raw.lower().strip()
            if input_mood in ("empty", "blank", "none", "no mood", "simple", "empty playlist"):
                flow.mood = False
                try:
                    await self.db.create_playlist(flow.name)
                    self._playlist_flow = None
                    return AssistantResponse(
                        spoken=f"{self._say('affirmative')} I have created the empty playlist '{flow.name}' for you.",
                        displayed=f"Created empty playlist: **{flow.name}**",
                    )
                except Exception as exc:
                    self._playlist_flow = None
                    return AssistantResponse(
                        spoken=f"I couldn't create that playlist: {exc}",
                        displayed=f"Failed to create playlist: {exc}",
                        success=False
                    )

            from utils import track_graph as tg
            matched_mood = None
            for m in tg.MOOD_PROFILES.keys():
                if input_mood == m.lower() or input_mood.startswith(m.lower()) or m.lower() in input_mood:
                    matched_mood = m
                    break
            
            if not matched_mood:
                return AssistantResponse(
                    spoken=f"I didn't recognize '{raw}' as a mood, sir. Should it be empty, or one of the known moods like chill, upbeat, or dark?",
                    displayed=f"Unknown mood **{raw}**. Try a known mood (e.g. *chill*) or *empty*.",
                )

            flow.mood = matched_mood
            return AssistantResponse(
                spoken=f"Understood. How many {matched_mood} songs should we include in '{flow.name}', sir? (Default is 20)",
                displayed=f"How many **{matched_mood}** songs should we include in **{flow.name}**? (Default: **20**)"
            )

        # Step 3: Get Limit
        if flow.limit is None:
            import re
            digit_match = re.search(r"\b\d+\b", raw)
            limit = 20
            if digit_match:
                limit = int(digit_match.group(0))
            else:
                word_to_num = {
                    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                    "fifteen": 15, "twenty": 20, "thirty": 30, "fifty": 50
                }
                for w, num in word_to_num.items():
                    if w in raw.lower():
                        limit = num
                        break
            
            flow.limit = limit
            mood = flow.mood
            name = flow.name

            from utils.auto_playlist import generate_mood_playlist
            tracks = await generate_mood_playlist(self.db, mood, target_length=limit)
            if not tracks:
                # Still create the empty playlist as a fallback
                try:
                    await self.db.create_playlist(name)
                except Exception:
                    pass
                self._playlist_flow = None
                return AssistantResponse(
                    spoken=(
                        f"I haven't analysed enough of your library to pick by mood yet. "
                        f"I've created the empty playlist '{name}' for you, sir."
                    ),
                    displayed=(
                        f"Created empty playlist **{name}**. Could not populate "
                        f"with **{mood}** tracks (run **rescan dsp** first)."
                    ),
                    success=False
                )
            
            try:
                playlist_id = await self.db.create_playlist(name)
                for t in tracks:
                    await self.db.add_track_to_playlist(playlist_id, t["path"])
                
                self._playlist_flow = None
                first = tracks[0]
                return AssistantResponse(
                    spoken=(
                        f"{self._say('affirmative')} I've built '{name}' with "
                        f"{len(tracks)} {mood} tracks, opening with "
                        f"{first.get('title') or 'the top match'}."
                    ),
                    displayed=(
                        f"Created **{name}** with **{len(tracks)}** {mood} tracks "
                        f"ranked over the library's DSP features."
                    ),
                )
            except Exception as exc:
                self._playlist_flow = None
                return AssistantResponse(
                    spoken=f"Failed to complete playlist creation, sir: {exc}",
                    displayed=f"Error: {exc}",
                    success=False
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
        listen-feedback signal (used by mood re-ranking) accumulate state
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
                    f"{', '.join(sorted(tg.MOODS.keys()))}."
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
        verb = intent.extras.get("verb")
        is_queue = verb and verb.lower().strip() in ("add", "queue", "enqueue", "put")

        if is_queue and self.engine.queue:
            for t in engine_tracks:
                self.engine.queue_last(t)
                self._remember(t["path"])
            return AssistantResponse(
                spoken=f"{self._say('affirmative')} Added {len(tracks)} {mood} tracks to the queue.",
                displayed=f"Queued **{len(tracks)}** {mood} tracks based on DSP profile.",
                extras={"mood": mood, "queued": len(tracks),
                        "entity_intent": "play_mood"},
            )
        else:
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
                extras={"mood": mood, "queued": len(tracks),
                        "entity_intent": "play_mood"},
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
 
        # One pooled walk over acoustic + artist tiers. The walk picks the
        # first step itself (restart probability + softmax handle anchoring
        # and exploration), and the multi-tier pool means we don't need a
        # separate "acoustic neighbours empty → fall back to artist" branch
        # at the seed; the walk does it implicitly per step.
        try:
            walk_paths = await track_graph.walk(
                self.db, seed_path,
                length=12,
                edge_kinds=(track_graph.KIND_ACOUSTIC, track_graph.KIND_ARTIST),
                avoid=avoid,
                restart_prob=0.15,
                diversity_lambda=0.3,
                temperature=0.08,
                teleport_path=seed_path,
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
        from utils import track_graph as tg
        canonical_moods = sorted(tg.MOODS.keys())
        custom_islets = tg.list_islets()

        spoken_msg = (
            "I can manage your playback, queue tracks, navigate by mood, create playlists, "
            "or walk the acoustic similarity graph, sir. Just say 'play some chill music' "
            "or 'more by this artist' to begin."
        )

        moods_str = ", ".join(canonical_moods)
        if custom_islets:
            islets_str = ", ".join(custom_islets)
        else:
            islets_str = "None registered yet. Save one by saying *save this as [name]* while a song plays."

        displayed_msg = (
            "### Jarvis System Capabilities\n\n"
            "*   **Playback**: `play [song/artist]`, `pause`, `resume`, `skip`, `prev`, `shuffle`\n"
            f"*   **Acoustic Moods**: `play [mood]` (Available: {moods_str})\n"
            f"*   **Custom Islets**: `play [islet]` (Available: {islets_str})\n"
            "*   **Similarity Graph**: `play similar`, `more like this`, `more by this artist`\n"
            "*   **Playlists**: `create playlist [name]`, "
            "`create [mood] playlist called [name]` (library-wide DSP-ranked, "
            "e.g. *create a chill playlist called Late Night*), "
            "`add this to [playlist]`\n"
            "*   **Sub-systems**: `rescan dsp`, `clear queue`, `download [song]`"
        )
        return AssistantResponse(spoken=spoken_msg, displayed=displayed_msg)

    async def _handle_create_mood(self, intent: ai.Intent) -> AssistantResponse:
        """Single-shot islet creation: the currently-playing track's timbre
        becomes the centroid; membership is computed on demand by
        `tracks_in_islet` with the configured cosine threshold."""
        from utils import track_graph as tg
        from utils.dsp import unpack_timbre

        name = (intent.query or "").strip().strip("\"'").strip()
        if not name:
            return AssistantResponse(
                spoken="What should we name the islet, sir?",
                displayed="Please specify a name for the islet (e.g. *save this as Sunday morning*).",
                success=False,
            )
        if name.lower() in tg.MOOD_PROFILES:
            return AssistantResponse(
                spoken=f"'{name}' is a built-in mood, sir. Pick a different name.",
                displayed=f"**{name}** is a built-in mood. Choose a different name.",
                success=False,
            )

        track_path = self.engine.current_path
        if not track_path:
            return AssistantResponse(
                spoken="Nothing is playing, sir. Start the track that should seed this islet first.",
                displayed="No current track. Play the exemplar track first, then say *save this as <name>*.",
                success=False,
            )

        row = await self.db.get_track_full(track_path)
        timbre = unpack_timbre(row.get("timbre")) if row else None
        if timbre is None:
            return AssistantResponse(
                spoken="The current track has not been DSP-analysed yet, sir.",
                displayed="Current track has no DSP features. Run a rescan first.",
                success=False,
            )

        try:
            tg.save_custom_mood(
                name,
                centroid=[float(x) for x in timbre],
                exemplar_path=track_path,
            )
            ai.register_dynamic_mood_vocabulary()
        except Exception as exc:
            logger.exception("Failed to save islet %s", name)
            return AssistantResponse(
                spoken=f"Failed to save the islet, sir: {exc}",
                displayed=f"Error saving islet: {exc}",
                success=False,
            )

        title = (row.get("title") if row else None) or "this track"
        return AssistantResponse(
            spoken=f"Islet '{name}' saved, sir, seeded by {title}.",
            displayed=f"Islet **{name}** saved — seeded by *{title}*.",
        )

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

        if mood:
            matched_mood = None
            for m in tg.MOOD_PROFILES.keys():
                if mood == m.lower():
                    matched_mood = m
                    break
            mood = matched_mood

        if not name or not mood or mood not in tg.MOOD_PROFILES:
            self._playlist_flow = PendingPlaylistCreation(
                name=name if name else None,
                mood=mood if (mood and mood in tg.MOOD_PROFILES) else None
            )
            if not name:
                return AssistantResponse(
                    spoken="What should we name the playlist, sir?",
                    displayed="Playlist name cannot be empty. Please specify a name:",
                )
            else:
                return AssistantResponse(
                    spoken="Should this be a smart playlist based on a mood, or a simple empty playlist, sir?",
                    displayed=(
                        "Should this be a smart playlist based on a mood, or a simple empty playlist?\n\n"
                        "**Smart Moods**: " + ", ".join(sorted(tg.MOODS.keys())) + " (or type **empty**)"
                    )
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
    ai.INTENT_CREATE_MOOD:     AssistantRunner._handle_create_mood,
    ai.INTENT_GREET:           AssistantRunner._handle_greet,
    ai.INTENT_HELP:          AssistantRunner._handle_help,
    ai.INTENT_UNKNOWN:       AssistantRunner._handle_unknown,
}
