"""
test_chat_memory.py — Unit tests for the Conversational Memory &
Pronoun/Anaphora Resolution feature in AssistantRunner.

Run from the StreamripApp directory:
    python -m unittest test_chat_memory
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Bootstrap: stub heavy optional modules BEFORE any project imports so that
# the import chain (AssistantRunner → track_graph → dsp → numpy) never
# executes module-level numpy code on a plain test environment.
# ---------------------------------------------------------------------------

def _stub_module(name: str, attrs: dict | None = None):
    """Insert a lightweight stub into sys.modules."""
    if name not in sys.modules:
        mod = types.ModuleType(name)
        if attrs:
            for k, v in attrs.items():
                setattr(mod, k, v)
        sys.modules[name] = mod
    return sys.modules[name]


# We use setUpModule and tearDownModule to dynamically inject and remove
# these stubs, preventing them from leaking into other test suites during
# pytest's parallel or sequential run phases.
_original_modules = {}

def setUpModule():
    # Save original modules if they exist in sys.modules
    for m in ["utils.dsp", "utils.track_graph", "flet", "flet_core"]:
        if m in sys.modules:
            _original_modules[m] = sys.modules[m]

    # Stub utils.dsp and utils.track_graph first — they pull in numpy at
    # module level (np.array, np.ndarray type hints, etc.).
    _stub_module("utils.dsp", {
        "FEATURES_VERSION": 3,
        "Features": type("Features", (), {}),
        "unpack_timbre": lambda blob: None,
    })
    _stub_module("utils.track_graph", {
        "KIND_ACOUSTIC": "acoustic",
        "KIND_ARTIST": "artist",
        "MOOD_PROFILES": {},   # legacy name kept for older code paths
        "MOODS": {},           # renamed in current codebase
        "neighbors": None,
        "walk": None,
        "graph_status": None,
        "build_metadata_edges": None,
        "build_acoustic_edges": None,
    })

    # Flet stubs
    for _m in ["flet", "flet_core"]:
        _stub_module(_m)



# ---------------------------------------------------------------------------
# Minimal engine / DB stubs
# ---------------------------------------------------------------------------

class _FakeEngine:
    """Bare-minimum audio engine stub."""
    def __init__(self, current_path: str = "", current_artist: str = ""):
        self.current_path = current_path
        self.current_artist = current_artist
        self.queue: list = []

    def set_queue(self, tracks, start_index=0):
        self.queue = list(tracks)

    def queue_last(self, track):
        self.queue.append(track)


class _FakeCursor:
    """Async context manager that yields one row whose artist matches params[0]."""
    def __init__(self, tracks, params):
        self._tracks = tracks
        self._params = params

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetchone(self):
        if not self._params:
            return None
        target = self._params[0].lower()
        for t in self._tracks:
            if (t.get("artist") or "").lower() == target:
                return {"path": t["path"], "title": t["title"], "artist": t["artist"]}
        return None


class _FakeConn:
    """Pretend aiosqlite connection that delegates to _FakeCursor."""
    def __init__(self, tracks):
        self._tracks = tracks

    def execute(self, sql, params=()):
        return _FakeCursor(self._tracks, params)


class _FakeDB:
    """Stub db_manager with configurable in-memory data."""

    def __init__(self, tracks=None, artists=None):
        self._tracks: list[dict] = tracks or []
        self._artists: list[dict] = artists or []

    async def search_tracks_simple(self, query: str, limit: int = 5) -> list[dict]:
        q = query.lower()
        results = [
            t for t in self._tracks
            if q in (t.get("title") or "").lower()
            or q in (t.get("artist") or "").lower()
            or q in (t.get("album") or "").lower()
        ]
        return results[:limit]

    async def get_all_tracks(self) -> list[dict]:
        return list(self._tracks)

    async def get_all_artists(self, search_query: str = "", sort_mode: str = "name") -> list[dict]:
        if not search_query:
            return list(self._artists)
        sq = search_query.lower()
        return [a for a in self._artists if sq in a["name"].lower()]

    async def get_track_full(self, path: str) -> dict | None:
        return next((t for t in self._tracks if t.get("path") == path), None)

    async def get_connection(self):
        return _FakeConn(self._tracks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_TRACKS = [
    {
        "path": "/music/yesterday.flac",
        "title": "Yesterday",
        "artist": "The Beatles",
        "album": "Help!",
        "duration": 125.0,
        "image_url": "",
    },
    {
        "path": "/music/comfortably_numb.flac",
        "title": "Comfortably Numb",
        "artist": "Pink Floyd",
        "album": "The Wall",
        "duration": 382.0,
        "image_url": "",
    },
    {
        "path": "/music/let_it_be.flac",
        "title": "Let It Be",
        "artist": "The Beatles",
        "album": "Let It Be",
        "duration": 243.0,
        "image_url": "",
    },
]

_SAMPLE_ARTISTS = [
    {"id": 1, "name": "The Beatles", "album_count": 12, "track_count": 213},
    {"id": 2, "name": "Pink Floyd",  "album_count": 15, "track_count": 180},
]


def _make_runner(tracks=None, artists=None,
                 engine_path: str = "", engine_artist: str = ""):
    """Return (AssistantRunner, engine, db) wired to stubs."""
    from utils.assistant_runner import AssistantRunner
    engine = _FakeEngine(current_path=engine_path, current_artist=engine_artist)
    db = _FakeDB(tracks=tracks or [], artists=artists or [])
    runner = AssistantRunner(db_manager=db, audio_engine=engine)
    return runner, engine, db


def _make_intent(name: str = "play_now", query: str = ""):
    from utils import assistant_intent as ai
    return ai.Intent(name=name, query=query, raw=query)


def _write_history(tmp_dir: str, messages: list) -> str:
    """Persist a minimal chat_history.json and return its path."""
    import time
    path = os.path.join(tmp_dir, "chat_history.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "messages": messages,
            "init_greeted": True,
            "last_active_timestamp": time.time(),
        }, f)
    return path


def run(coro):
    """Synchronous helper to drive a coroutine in the current event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _run_anaphora(runner, messages: list, intent):
    """
    Patch ChatMemoryManager to serve *messages* from a temp file,
    then run _resolve_anaphora and return (result_intent, engine, confirm_q).
    """
    from utils.chat_memory import ChatMemoryManager

    with tempfile.TemporaryDirectory() as tmp_dir:
        hist_path = _write_history(tmp_dir, messages)

        def _fake_path(self_inner):
            return hist_path

        with patch.object(ChatMemoryManager, "_get_history_path", _fake_path):
            result_intent, confirm_q = run(runner._resolve_anaphora(intent))
    return result_intent, runner.engine, confirm_q


# ===========================================================================
# Test classes
# ===========================================================================


class TestAnaphoraTriggerDetection(unittest.TestCase):
    """_resolve_anaphora activates only for recognised trigger words."""

    TRIGGER_WORDS = [
        "it", "this", "that", "them", "their",
        "the song", "the artist", "the track", "the tracks",
        "the music", "the band", "the album",
    ]

    NON_TRIGGERS = [
        "comfortably numb",
        "pink floyd",
        "yesterday",
        "",
        "play random",
        "bohemian rhapsody",
    ]

    def test_all_trigger_words_recognised(self):
        """Every word in _ANAPHORA_TRIGGERS should be present in the class constant."""
        from utils.assistant_runner import AssistantRunner
        for word in self.TRIGGER_WORDS:
            self.assertIn(word, AssistantRunner._ANAPHORA_TRIGGERS,
                          f"'{word}' not in _ANAPHORA_TRIGGERS")

    def test_concrete_query_bypasses_anaphora(self):
        """A specific title must pass straight through without any DB call."""
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS)
        for query in self.NON_TRIGGERS:
            with self.subTest(query=query):
                intent = _make_intent(query=query)
                result, _ = run(runner._resolve_anaphora(intent))
                # query unchanged because resolution was skipped
                self.assertEqual(result.query, query)

    def test_trigger_word_alone_attempts_resolution(self):
        """A bare trigger with no history returns gracefully (no raise)."""
        runner, _, _ = _make_runner()
        for word in ["it", "them", "the song"]:
            with self.subTest(word=word):
                intent = _make_intent(query=word)
                result, _ = run(runner._resolve_anaphora(intent))
                self.assertIsNotNone(result)

    def test_play_similar_no_path_is_implicit_trigger(self):
        """play_similar with empty engine.current_path is an implicit trigger."""
        runner, engine, _ = _make_runner()
        self.assertEqual(engine.current_path, "")
        intent = _make_intent(name="play_similar", query="")
        # No history available → must not raise and returns unchanged intent
        result, _ = run(runner._resolve_anaphora(intent))
        self.assertIsNotNone(result)

    def test_play_more_by_no_path_is_implicit_trigger(self):
        """play_more_by with empty engine.current_path is an implicit trigger."""
        runner, engine, _ = _make_runner()
        intent = _make_intent(name="play_more_by", query="")
        result, _ = run(runner._resolve_anaphora(intent))
        self.assertIsNotNone(result)

    def test_play_similar_with_path_not_triggered(self):
        """play_similar with a live current_path must NOT scan history."""
        runner, engine, _ = _make_runner(
            tracks=_SAMPLE_TRACKS,
            engine_path="/music/yesterday.flac",
            engine_artist="The Beatles",
        )
        intent = _make_intent(name="play_similar", query="")
        result, _ = run(runner._resolve_anaphora(intent))
        # query must stay empty — resolution was skipped
        self.assertEqual(result.query, "")

    def test_play_more_by_with_path_not_triggered(self):
        """play_more_by when engine already has a path must NOT scan history."""
        runner, engine, _ = _make_runner(
            tracks=_SAMPLE_TRACKS,
            engine_path="/music/comfortably_numb.flac",
            engine_artist="Pink Floyd",
        )
        intent = _make_intent(name="play_more_by", query="")
        result, _ = run(runner._resolve_anaphora(intent))
        self.assertEqual(result.query, "")


class TestBoldTagParsing(unittest.TestCase):
    """Bold-tag regex extracts track and artist names correctly."""

    _BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")

    def _parse(self, text: str) -> tuple[str | None, str | None]:
        """Mirror the extraction logic inside _resolve_anaphora."""
        hits = self._BOLD_RE.findall(text)
        title, artist = None, None
        for hit in hits:
            if " — " in hit or " - " in hit:
                sep = " — " if " — " in hit else " - "
                parts = hit.split(sep, 1)
                if len(parts) == 2:
                    title = title or parts[0].strip()
                    artist = artist or parts[1].strip()
                else:
                    title = title or hit.strip()
            else:
                if title and not artist:
                    artist = hit.strip()
                else:
                    title = title or hit.strip()
        return title, artist

    def test_em_dash_separator(self):
        t, a = self._parse("Playing **Yesterday — The Beatles** from your library.")
        self.assertEqual(t, "Yesterday")
        self.assertEqual(a, "The Beatles")

    def test_hyphen_separator(self):
        t, a = self._parse("Now playing **Comfortably Numb - Pink Floyd**.")
        self.assertEqual(t, "Comfortably Numb")
        self.assertEqual(a, "Pink Floyd")

    def test_single_bold_tag_becomes_title(self):
        t, a = self._parse("I found **Bohemian Rhapsody** in your library.")
        self.assertEqual(t, "Bohemian Rhapsody")
        self.assertIsNone(a)

    def test_two_separate_bold_tags_title_then_artist(self):
        t, a = self._parse("Playing **Yesterday** by **The Beatles**.")
        self.assertEqual(t, "Yesterday")
        self.assertEqual(a, "The Beatles")

    def test_numeric_bold_tag_does_not_crash(self):
        """Numeric bold (e.g. track count) must not raise."""
        t, a = self._parse("Queued **5** tracks for you.")
        self.assertIsNotNone(t)  # "5" captured — harmless

    def test_no_bold_tags_returns_nones(self):
        t, a = self._parse("Playing something from your library.")
        self.assertIsNone(t)
        self.assertIsNone(a)

    def test_multiword_artist_with_em_dash(self):
        t, a = self._parse("Playing **Wish You Were Here — Pink Floyd**.")
        self.assertEqual(t, "Wish You Were Here")
        self.assertEqual(a, "Pink Floyd")

    def test_first_bold_entity_wins_for_title(self):
        """When two separate bold entities appear, only first is title."""
        t, a = self._parse("**Song A** and **Song B** are queued.")
        self.assertEqual(t, "Song A")
        self.assertEqual(a, "Song B")


class TestBackwardScanCurrentTurnSkipping(unittest.TestCase):
    """The current user turn (last message) must always be skipped."""

    def test_current_user_turn_is_skipped(self):
        """The current 'play it' is at index [-1] and must be skipped."""
        messages = [
            {"sender": "user",      "text": "search for yesterday"},
            {"sender": "assistant", "text": "Playing **Yesterday — The Beatles**."},
            {"sender": "user",      "text": "play it"},   # ← current turn
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_now", query="it")
        result, _, _ = _run_anaphora(runner, messages, intent)
        # Must have resolved to Yesterday, not "play it"
        self.assertNotEqual(result.query.lower(), "it")
        self.assertIn("Yesterday", result.query)

    def test_empty_history_returns_unchanged(self):
        """Zero messages → no crash, intent unchanged."""
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS)
        intent = _make_intent(query="it")
        result, _, _ = _run_anaphora(runner, [], intent)
        self.assertEqual(result.query, "it")

    def test_only_current_user_turn_no_resolve(self):
        """Only the current user message in history → nothing to scan."""
        messages = [{"sender": "user", "text": "play it"}]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS)
        intent = _make_intent(query="it")
        result, _, _ = _run_anaphora(runner, messages, intent)
        # Nothing resolved — query stays as the trigger word
        self.assertEqual(result.query, "it")

    def test_assistant_turn_not_skipped(self):
        """Assistant messages immediately before the user turn are scanned."""
        messages = [
            {"sender": "assistant", "text": "Playing **Comfortably Numb — Pink Floyd**."},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(query="it")
        result, _, _ = _run_anaphora(runner, messages, intent)
        self.assertIn("Comfortably Numb", result.query)


class TestTrackResolution(unittest.TestCase):
    """Resolved track names replace the anaphoric query."""

    def test_it_resolves_to_last_played_track(self):
        messages = [
            {"sender": "user",      "text": "search for yesterday"},
            {"sender": "assistant", "text": "Playing **Yesterday — The Beatles** from your library."},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Yesterday", result.query)
        self.assertIn("Beatles", result.query)

    def test_the_song_resolves_to_last_track(self):
        messages = [
            {"sender": "assistant", "text": "Now playing **Comfortably Numb — Pink Floyd**."},
            {"sender": "user",      "text": "add the song to queue"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="the song"))
        self.assertIn("Comfortably Numb", result.query)

    def test_this_resolves_correctly(self):
        messages = [
            {"sender": "assistant", "text": "Playing **Let It Be — The Beatles**."},
            {"sender": "user",      "text": "play more like this"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="this"))
        self.assertIn("Let It Be", result.query)

    def test_that_resolves_correctly(self):
        messages = [
            {"sender": "assistant", "text": "I found **Yesterday — The Beatles**."},
            {"sender": "user",      "text": "queue that"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="that"))
        self.assertIn("Yesterday", result.query)

    def test_unrecognised_entity_leaves_query_unchanged(self):
        """If the bolded entity isn't in the DB, query is not changed."""
        messages = [
            {"sender": "assistant", "text": "Playing **Some Unknown Track — Unknown Artist**."},
            {"sender": "user",      "text": "play it"},
        ]
        # DB has no such track
        runner, _, _ = _make_runner(tracks=[], artists=[])
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        # Resolution failed gracefully — trigger word remains
        self.assertEqual(result.query, "it")


class TestArtistResolution(unittest.TestCase):
    """Artist-only references (them / their) resolve via get_all_artists."""

    def test_them_resolves_to_last_artist(self):
        messages = [
            {"sender": "user",      "text": "play comfortably numb"},
            {"sender": "assistant", "text": "Playing Comfortably Numb.",
             "entities": {"track": _SAMPLE_TRACKS[1], "artist": "Pink Floyd",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(name="play_more_by", query="them"))
        self.assertEqual(result.query, "Pink Floyd",
                         f"Artist not resolved; query={result.query!r}")

    def test_their_resolves_to_last_artist(self):
        messages = [
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play their other songs"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="their"))
        self.assertEqual(result.query, "The Beatles",
                         f"Artist 'The Beatles' not reflected in query: {result.query!r}")

    def test_artist_only_bold_tag_resolves(self):
        """Artist-only entity dict resolves for play_more_by."""
        messages = [
            {"sender": "assistant", "text": "Here is info about The Beatles.",
             "entities": {"track": None, "artist": "The Beatles",
                          "playlist": None, "intent": "search_artist"}},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(name="play_more_by", query="them"))
        self.assertIn("The Beatles", result.query)


class TestUserQueryFallback(unittest.TestCase):
    """Behaviour when assistant messages lack both entities and bold tags."""

    def test_no_bold_no_entity_returns_unchanged(self):
        """Assistant message with no entities dict and no bold tags → no resolution."""
        messages = [
            {"sender": "user",      "text": "comfortably numb"},
            # Assistant reply has neither entities nor bold tags
            {"sender": "assistant", "text": "I found a match in your library."},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        # The new design only scans assistant messages; the assistant bubble here
        # has no parseable entity → resolution cannot proceed.
        self.assertEqual(result.query, "it",
                         "Should stay as trigger word when no entity can be found.")

    def test_bold_takes_priority_over_user_text(self):
        """When a bold entity is available, it must be used (not user text)."""
        messages = [
            {"sender": "user",      "text": "pink floyd"},           # prior user query
            {"sender": "assistant", "text": "Playing **Yesterday — The Beatles**."},  # bold
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        # Should use "Yesterday", NOT "pink floyd"
        self.assertIn("Yesterday", result.query)

    def test_entity_dict_always_beats_user_text(self):
        """entities dict takes priority over anything in the user's prior message."""
        entities = {
            "track": _SAMPLE_TRACKS[0],   # Yesterday
            "artist": "The Beatles",
            "playlist": None,
            "intent": "play_now",
        }
        messages = [
            {"sender": "user",      "text": "comfortably numb"},   # red herring
            {"sender": "assistant", "text": "Playing Yesterday.", "entities": entities},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Yesterday", result.query,
                      "entities dict should take priority over user text.")



class TestEngineSeedingWhenStopped(unittest.TestCase):
    """When nothing is playing and a similarity intent arrives, the engine must be seeded."""

    def test_play_similar_seeds_engine_current_path(self):
        messages = [
            {"sender": "user",      "text": "play let it be"},
            {"sender": "assistant", "text": "Playing **Let It Be — The Beatles**."},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        self.assertEqual(engine.current_path, "")
        intent = _make_intent(name="play_similar", query="")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertNotEqual(resolved_intent.extras.get("seed_path_override", ""), "",
                            "seed_path_override was not set for play_similar.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_play_more_by_seeds_engine_current_path(self):
        messages = [
            {"sender": "user",      "text": "play yesterday"},
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_more_by", query="them")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertNotEqual(resolved_intent.extras.get("seed_path_override", ""), "",
                            "seed_path_override was not set for play_more_by.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_engine_artist_set_alongside_path(self):
        messages = [
            {"sender": "assistant", "text": "Playing Comfortably Numb.",
             "entities": {"track": _SAMPLE_TRACKS[1], "artist": "Pink Floyd",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_more_by", query="them")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertNotEqual(resolved_intent.extras.get("seed_artist_override", ""), "",
                            "seed_artist_override was not set alongside seed_path_override.")
        self.assertEqual(engine_after.current_artist, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_seed_from_artist_only_bold_tag(self):
        """Only artist in bold (no track) → DB lookup seeds the engine."""
        messages = [
            {"sender": "assistant", "text": "Here is info about **The Beatles**."},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_similar", query="")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertNotEqual(resolved_intent.extras.get("seed_path_override", ""), "",
                            "seed_path_override not seeded from artist-only bold tag.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_engine_not_overwritten_when_already_playing(self):
        """If engine already has a current_path, it must NOT be overwritten."""
        original_path = "/music/comfortably_numb.flac"
        messages = [
            {"sender": "assistant", "text": "Playing **Yesterday — The Beatles**."},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(
            tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS,
            engine_path=original_path, engine_artist="Pink Floyd",
        )
        intent = _make_intent(name="play_similar", query="")
        # Resolution should be skipped (current_path is set → no implicit context)
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertEqual(engine_after.current_path, original_path,
                         "engine.current_path was wrongly overwritten.")
        self.assertIsNone(resolved_intent.extras.get("seed_path_override"),
                          "seed_path_override set when current_path already active.")


class TestEntityDictFastPath(unittest.TestCase):
    """Fast-path: messages that carry an 'entities' dict resolve without DB lookups."""

    # Structured entity dicts that main.py now persists with every assistant bubble
    _ENTITIES_YESTERDAY = {
        "track": {
            "path": "/music/yesterday.flac",
            "title": "Yesterday",
            "artist": "The Beatles",
            "album": "Help!",
            "duration": 125.0,
            "image_url": "",
        },
        "artist": "The Beatles",
        "playlist": None,
        "intent": "play_now",
    }
    _ENTITIES_COMFORTABLY_NUMB = {
        "track": {
            "path": "/music/comfortably_numb.flac",
            "title": "Comfortably Numb",
            "artist": "Pink Floyd",
            "album": "The Wall",
            "duration": 382.0,
            "image_url": "",
        },
        "artist": "Pink Floyd",
        "playlist": None,
        "intent": "play_now",
    }
    _ENTITIES_ARTIST_ONLY = {
        "track": None,
        "artist": "The Beatles",
        "playlist": None,
        "intent": "search_artist",
    }

    def test_entity_dict_track_used_directly(self):
        """When entities.track is present the resolver must NOT call the DB."""
        messages = [
            {"sender": "user",      "text": "play yesterday"},
            {"sender": "assistant", "text": "Playing **Yesterday — The Beatles**.",
             "entities": self._ENTITIES_YESTERDAY},
            {"sender": "user",      "text": "play it"},
        ]
        # DB is empty — if the fast path reads from it, the track won't be found
        runner, _, _ = _make_runner(tracks=[], artists=[])
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Yesterday", result.query,
                      "Entity-dict fast path didn't use entities.track directly.")

    def test_entity_dict_artist_only_resolves_for_play_more_by(self):
        """Artist-only entity dict seeds engine for play_more_by."""
        messages = [
            {"sender": "assistant", "text": "Here is info about The Beatles.",
             "entities": self._ENTITIES_ARTIST_ONLY},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_more_by", query="them")
        result, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertEqual(result.query, "The Beatles",
                         "Artist-only entity dict did not resolve to artist name.")

    def test_entity_dict_takes_priority_over_bold_tags(self):
        """entities dict must win over bold-markdown parsing in same message."""
        messages = [
            {"sender": "user",      "text": "play yesterday"},
            # entities says Comfortably Numb; bold text says Yesterday — dict wins
            {"sender": "assistant",
             "text": "Playing **Yesterday — The Beatles**.",
             "entities": self._ENTITIES_COMFORTABLY_NUMB},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Comfortably Numb", result.query,
                      "entities dict should have taken priority over bold-tag text.")

    def test_most_recent_entity_dict_wins(self):
        """Of multiple entity-dict messages, the most recent one is used."""
        messages = [
            {"sender": "user",      "text": "play yesterday"},
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": self._ENTITIES_YESTERDAY},
            {"sender": "user",      "text": "actually play comfortably numb"},
            {"sender": "assistant", "text": "Playing Comfortably Numb.",
             "entities": self._ENTITIES_COMFORTABLY_NUMB},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Comfortably Numb", result.query,
                      "Most-recent entity dict not selected.")

    def test_entity_dict_seeds_engine_for_play_similar(self):
        """Entity-dict track path is used to seed engine.current_path."""
        messages = [
            {"sender": "assistant", "text": "Playing Comfortably Numb.",
             "entities": self._ENTITIES_COMFORTABLY_NUMB},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        self.assertEqual(engine.current_path, "")
        intent = _make_intent(name="play_similar", query="")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertEqual(resolved_intent.extras.get("seed_path_override"), "/music/comfortably_numb.flac",
                         "seed_path_override not set from entities.track.path.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_null_track_in_entity_dict_falls_through_to_artist(self):
        """If entities.track is None, must fall through to entities.artist."""
        entities = {"track": None, "artist": "Pink Floyd", "playlist": None, "intent": "search"}
        messages = [
            {"sender": "assistant", "text": "Here is Pink Floyd.", "entities": entities},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages,
                                  _make_intent(name="play_more_by", query="them"))
        self.assertEqual(result.query, "Pink Floyd",
                         "Should have fallen through to entities.artist when track is None.")

    def test_legacy_fallback_active_for_messages_without_entities(self):
        """Messages without an 'entities' key still resolve via bold-tag parsing."""
        messages = [
            # No 'entities' key — old-format message
            {"sender": "assistant", "text": "Playing **Yesterday — The Beatles**."},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Yesterday", result.query,
                      "Legacy bold-tag fallback not working for messages without entities.")

    def test_none_entities_value_falls_back_to_legacy(self):
        """entities=None in the message triggers the legacy bold-tag path."""
        messages = [
            {"sender": "assistant",
             "text": "Playing **Let It Be — The Beatles**.",
             "entities": None},
            {"sender": "user", "text": "queue it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Let It Be", result.query,
                      "None-entities should fall back to bold-tag parsing.")




class TestPronounTypeHardFilter(unittest.TestCase):
    """Pronoun → entity-type mapping with hard filter: no cross-type fallback."""

    # --- class constant coverage -------------------------------------------

    def test_anaphora_triggers_is_superset_of_pronoun_type(self):
        """_ANAPHORA_TRIGGERS must equal frozenset(_PRONOUN_TYPE)."""
        from utils.assistant_runner import AssistantRunner
        self.assertEqual(
            AssistantRunner._ANAPHORA_TRIGGERS,
            frozenset(AssistantRunner._PRONOUN_TYPE),
        )

    def test_new_triggers_present(self):
        """Triggers added in the refactor must all be present."""
        from utils.assistant_runner import AssistantRunner
        for word in ["they", "this song", "that song", "this one",
                     "that one", "the same artist", "this album",
                     "those", "these", "those tracks"]:
            with self.subTest(word=word):
                self.assertIn(word, AssistantRunner._ANAPHORA_TRIGGERS)

    # --- regex extraction --------------------------------------------------

    def _trigger_for(self, query: str) -> str | None:
        from utils.assistant_runner import AssistantRunner
        import re
        m = AssistantRunner._ANAPHORA_RE.search(query.lower())
        return re.sub(r"\s+", " ", m.group(0).strip().lower()) if m else None

    def _type_for(self, query: str) -> str | None:
        from utils.assistant_runner import AssistantRunner
        t = self._trigger_for(query)
        return AssistantRunner._PRONOUN_TYPE.get(t) if t else None

    def test_it_is_track(self):          self.assertEqual(self._type_for("play it"), "track")
    def test_this_is_track(self):        self.assertEqual(self._type_for("play this"), "track")
    def test_that_is_track(self):        self.assertEqual(self._type_for("queue that"), "track")
    def test_this_song_is_track(self):   self.assertEqual(self._type_for("add this song"), "track")
    def test_that_song_is_track(self):   self.assertEqual(self._type_for("play that song"), "track")
    def test_this_one_is_track(self):    self.assertEqual(self._type_for("play this one"), "track")
    def test_that_one_is_track(self):    self.assertEqual(self._type_for("play that one"), "track")
    def test_the_song_is_track(self):    self.assertEqual(self._type_for("the song please"), "track")
    def test_the_track_is_track(self):   self.assertEqual(self._type_for("the track"), "track")
    def test_the_music_is_track(self):   self.assertEqual(self._type_for("restart the music"), "track")

    def test_them_is_artist(self):       self.assertEqual(self._type_for("play more by them"), "artist")
    def test_they_is_artist(self):       self.assertEqual(self._type_for("what did they release"), "artist")
    def test_their_is_artist(self):      self.assertEqual(self._type_for("play their albums"), "artist")
    def test_the_band_is_artist(self):   self.assertEqual(self._type_for("more from the band"), "artist")
    def test_the_artist_is_artist(self): self.assertEqual(self._type_for("info on the artist"), "artist")
    def test_same_artist_is_artist(self): self.assertEqual(self._type_for("more by the same artist"), "artist")

    def test_the_album_is_album(self):   self.assertEqual(self._type_for("play the album"), "album")
    def test_this_album_is_album(self):  self.assertEqual(self._type_for("queue this album"), "album")

    def test_those_is_multi(self):       self.assertEqual(self._type_for("play those"), "multi")
    def test_these_is_multi(self):       self.assertEqual(self._type_for("shuffle these"), "multi")
    def test_those_tracks_is_multi(self): self.assertEqual(self._type_for("add those tracks"), "multi")
    def test_the_tracks_is_multi(self):  self.assertEqual(self._type_for("queue the tracks"), "multi")

    def test_non_trigger_returns_none(self):
        for q in ["yesterday", "pink floyd", "play random", ""]:
            with self.subTest(q=q):
                self.assertIsNone(self._type_for(q))

    # --- hard filter: wrong-type entity must NOT be returned ---------------

    def test_them_does_not_resolve_to_track(self):
        """'them' must not resolve when only a track entity is in history."""
        # history has a track entity but NO artist entity
        track_only_entities = {
            "track": _SAMPLE_TRACKS[0],
            "artist": None,
            "playlist": None,
            "intent": "play_now",
        }
        messages = [
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": track_only_entities},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages,
                                  _make_intent(name="play_more_by", query="them"))
        # Hard filter: no artist in history → intent unchanged
        self.assertEqual(result.query, "them",
                         "Hard filter failed: 'them' resolved from a track entity.")

    def test_it_does_not_resolve_to_artist(self):
        """'it' must not resolve when only an artist entity is in history."""
        artist_only_entities = {
            "track": None,
            "artist": "Pink Floyd",
            "playlist": None,
            "intent": "search_artist",
        }
        messages = [
            {"sender": "assistant", "text": "Info on Pink Floyd.",
             "entities": artist_only_entities},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        # Hard filter: no track in history → intent unchanged
        self.assertEqual(result.query, "it",
                         "Hard filter failed: 'it' resolved from an artist entity.")

    def test_album_type_does_not_resolve_to_track(self):
        """'the album' must not resolve when only a track entity is in history."""
        messages = [
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play the album"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="the album"))
        # No album entity in history → query unchanged
        self.assertEqual(result.query, "the album")

    def test_multi_type_leaves_intent_unchanged(self):
        """'those' / 'these' currently have no resolution path → unchanged."""
        messages = [
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "shuffle those"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="those"))
        self.assertEqual(result.query, "those")

    # --- scan window cap ---------------------------------------------------

    def test_scan_capped_at_12_messages(self):
        """Entity beyond 12 messages back must not be found."""
        entity_msg = {
            "sender": "assistant",
            "text": "Playing Yesterday.",
            "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                         "playlist": None, "intent": "play_now"},
        }
        # 13 filler messages between entity and current user turn
        filler = [{"sender": "user", "text": "what time is it"},
                  {"sender": "assistant", "text": "It is 3pm, sir."}] * 7
        messages = [entity_msg] + filler + [{"sender": "user", "text": "play it"}]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        # Entity is > 12 messages back → not found → query unchanged
        self.assertEqual(result.query, "it",
                         "Scan window exceeded 12 messages.")

    def test_entity_within_12_messages_is_found(self):
        """Entity exactly within the 12-message window must be resolved."""
        entity_msg = {
            "sender": "assistant",
            "text": "Playing Comfortably Numb.",
            "entities": {"track": _SAMPLE_TRACKS[1], "artist": "Pink Floyd",
                         "playlist": None, "intent": "play_now"},
        }
        # 5 filler pairs (10 messages) between entity and current user turn
        filler = [{"sender": "user", "text": "what time is it"},
                  {"sender": "assistant", "text": "It is 3pm, sir."}] * 5
        messages = [entity_msg] + filler + [{"sender": "user", "text": "play it"}]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Comfortably Numb", result.query,
                      "Entity within the 12-message window was not resolved.")

    # --- interleaved non-music turns must not poison resolution ------------

    def test_non_music_turns_between_entity_and_trigger(self):
        """
        Classic adversarial case from plan §4:
          user: play hey jude
          assistant: Playing **Hey Jude — The Beatles**.
          user: what time is it
          assistant: It is 3pm, sir.
          user: play more by them
        The 3pm reply has no entities, but the scanner must skip it and
        still find the artist entity from the music reply.
        """
        messages = [
            {"sender": "user",      "text": "play hey jude"},
            {"sender": "assistant", "text": "Playing Hey Jude.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "what time is it"},
            {"sender": "assistant", "text": "It is 3pm, sir."},   # no entities
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(
            runner, messages,
            _make_intent(name="play_more_by", query="them"),
        )
        self.assertEqual(result.query, "The Beatles",
                         "Non-music interlude poisoned artist resolution.")

    def test_most_recent_typed_entity_wins(self):
        """
        Two music turns: first → Yesterday (The Beatles), second → Pink Floyd.
        'them' must resolve to Pink Floyd (most recent artist entity).
        """
        messages = [
            {"sender": "user",      "text": "play yesterday"},
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play comfortably numb"},
            {"sender": "assistant", "text": "Playing Comfortably Numb.",
             "entities": {"track": _SAMPLE_TRACKS[1], "artist": "Pink Floyd",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play more by them"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(
            runner, messages,
            _make_intent(name="play_more_by", query="them"),
        )
        self.assertEqual(result.query, "Pink Floyd",
                         "Most-recent artist entity not selected.")

    # --- implicit intent type inference ------------------------------------

    def test_play_similar_infers_track_type(self):
        """play_similar with no pronoun infers hint_type='track' via INTENT_DEFAULT_TYPE."""
        messages = [
            {"sender": "assistant", "text": "Playing Yesterday.",
             "entities": {"track": _SAMPLE_TRACKS[0], "artist": "The Beatles",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_similar", query="")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertEqual(resolved_intent.extras.get("seed_path_override"), "/music/yesterday.flac",
                         "seed_path_override did not infer track type from INTENT_DEFAULT_TYPE.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_play_more_by_infers_artist_type(self):
        """play_more_by with no pronoun infers hint_type='artist' via INTENT_DEFAULT_TYPE."""
        messages = [
            {"sender": "assistant", "text": "Playing Comfortably Numb.",
             "entities": {"track": _SAMPLE_TRACKS[1], "artist": "Pink Floyd",
                          "playlist": None, "intent": "play_now"}},
            {"sender": "user",      "text": "play more"},   # no pronoun
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_more_by", query="")
        result, _, _ = _run_anaphora(runner, messages, intent)
        self.assertEqual(result.query, "Pink Floyd",
                         "play_more_by did not infer artist type from INTENT_DEFAULT_TYPE.")


class TestAnaphoraConfirmationThreshold(unittest.TestCase):
    """When the resolved entity is found > _ANAPHORA_CONFIRM_THRESHOLD messages
    back, _resolve_anaphora must return a confirmation question instead of
    silently acting. The intent is still fully resolved so the callback can
    dispatch it directly on confirmation."""

    _ENTITY_YESTERDAY = {
        "track": _SAMPLE_TRACKS[0],
        "artist": "The Beatles",
        "playlist": None,
        "intent": "play_now",
    }
    _ENTITY_PINK_FLOYD = {
        "track": None,
        "artist": "Pink Floyd",
        "playlist": None,
        "intent": "search_artist",
    }

    def _build_messages(self, entity_msg: dict, filler_pairs: int) -> list:
        """entity_msg + N filler exchange pairs + current user turn."""
        filler = [
            {"sender": "user",      "text": "what time is it"},
            {"sender": "assistant", "text": "It is 3pm, sir."},
        ] * filler_pairs
        return [entity_msg] + filler + [{"sender": "user", "text": "play it"}]

    # -- below threshold: no question ----------------------------------------

    def test_near_entity_no_confirm(self):
        """Entity at rank <= threshold must resolve silently (confirm_q is None)."""
        from utils.assistant_runner import AssistantRunner
        threshold = AssistantRunner._ANAPHORA_CONFIRM_THRESHOLD
        entity_msg = {
            "sender": "assistant",
            "text": "Playing Yesterday.",
            "entities": self._ENTITY_YESTERDAY,
        }
        # 2 filler pairs = 4 messages + skip current user = entity at rank 4
        messages = self._build_messages(entity_msg, filler_pairs=2)
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, confirm_q = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIsNone(confirm_q,
                          f"Expected no confirmation for entity within threshold; got {confirm_q!r}")
        self.assertIn("Yesterday", result.query,
                      "Near entity should still resolve the query.")

    # -- above threshold: confirmation question returned ---------------------

    def test_far_track_returns_confirm_q(self):
        """Entity beyond threshold must produce a non-None confirm_q."""
        from utils.assistant_runner import AssistantRunner
        threshold = AssistantRunner._ANAPHORA_CONFIRM_THRESHOLD
        entity_msg = {
            "sender": "assistant",
            "text": "Playing Yesterday.",
            "entities": self._ENTITY_YESTERDAY,
        }
        # threshold + 1 filler pairs = entity at rank > threshold
        messages = self._build_messages(entity_msg, filler_pairs=threshold)
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, confirm_q = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIsNotNone(confirm_q,
                             "Expected confirmation question for far-back entity.")

    def test_far_track_question_mentions_title(self):
        """The confirmation question must name the track."""
        from utils.assistant_runner import AssistantRunner
        threshold = AssistantRunner._ANAPHORA_CONFIRM_THRESHOLD
        entity_msg = {
            "sender": "assistant",
            "text": "Playing Yesterday.",
            "entities": self._ENTITY_YESTERDAY,
        }
        messages = self._build_messages(entity_msg, filler_pairs=threshold)
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        _, _, confirm_q = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Yesterday", confirm_q,
                      f"Track name missing from question: {confirm_q!r}")

    def test_far_artist_returns_confirm_q(self):
        """Artist entity beyond threshold must also trigger a question."""
        from utils.assistant_runner import AssistantRunner
        threshold = AssistantRunner._ANAPHORA_CONFIRM_THRESHOLD
        entity_msg = {
            "sender": "assistant",
            "text": "Info on Pink Floyd.",
            "entities": self._ENTITY_PINK_FLOYD,
        }
        messages = self._build_messages(entity_msg, filler_pairs=threshold)
        # Override final user turn trigger to "them"
        messages[-1] = {"sender": "user", "text": "play more by them"}
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, confirm_q = _run_anaphora(
            runner, messages,
            _make_intent(name="play_more_by", query="them"),
        )
        self.assertIsNotNone(confirm_q)
        self.assertIn("Pink Floyd", confirm_q,
                      f"Artist name missing from question: {confirm_q!r}")

    def test_far_entity_intent_still_resolved(self):
        """Even when confirm_q is set, the intent must be fully resolved so the
        on_yes_callback can dispatch it immediately."""
        from utils.assistant_runner import AssistantRunner
        threshold = AssistantRunner._ANAPHORA_CONFIRM_THRESHOLD
        entity_msg = {
            "sender": "assistant",
            "text": "Playing Yesterday.",
            "entities": self._ENTITY_YESTERDAY,
        }
        messages = self._build_messages(entity_msg, filler_pairs=threshold)
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, confirm_q = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIsNotNone(confirm_q)
        self.assertIn("Yesterday", result.query,
                      "Intent must be fully resolved even when confirmation is pending.")


class TestEndToEndFlow(unittest.TestCase):
    """Integration-style tests simulating realistic conversation snippets."""

    def test_search_then_play_it(self):
        """
        Jarvis: 'Playing **Yesterday — The Beatles**'
        User:   'play it'  ->  resolved query = 'Yesterday The Beatles'
        """
        messages = [
            {"sender": "user",      "text": "search for yesterday"},
            {"sender": "assistant", "text": "Playing **Yesterday \u2014 The Beatles** from your library."},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(name="play_now", query="it"))
        self.assertIn("Yesterday", result.query)

    def test_play_then_request_similar_while_stopped(self):
        """Engine stops. User: 'play similar' -> engine seeded from history."""
        messages = [
            {"sender": "user",      "text": "play comfortably numb"},
            {"sender": "assistant", "text": "Playing **Comfortably Numb \u2014 Pink Floyd**."},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        intent = _make_intent(name="play_similar", query="")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertNotEqual(resolved_intent.extras.get("seed_path_override", ""), "",
                            "seed_path_override was not set from history context.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")

    def test_add_that_to_queue(self):
        """User: 'add that to queue' -> query resolved to 'Let It Be The Beatles'."""
        messages = [
            {"sender": "assistant", "text": "I found **Let It Be \u2014 The Beatles** in your library."},
            {"sender": "user",      "text": "add that to queue"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(name="queue_add", query="that"))
        self.assertIn("Let It Be", result.query)

    def test_multi_turn_uses_most_recent_entity(self):
        """Multi-turn: most recent entity dict / bold tag must win."""
        messages = [
            {"sender": "user",      "text": "play yesterday"},
            {"sender": "assistant", "text": "Playing **Yesterday \u2014 The Beatles**."},
            {"sender": "user",      "text": "actually play comfortably numb"},
            {"sender": "assistant", "text": "Playing **Comfortably Numb \u2014 Pink Floyd**."},
            {"sender": "user",      "text": "play it"},
        ]
        runner, _, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=_SAMPLE_ARTISTS)
        result, _, _ = _run_anaphora(runner, messages, _make_intent(query="it"))
        self.assertIn("Comfortably Numb", result.query,
                      "Multi-turn scan did not pick the most recent entity.")

    def test_entity_dict_end_to_end_no_db(self):
        """Full flow using entity-dict messages: DB empty but resolution succeeds."""
        entities = {
            "track": {
                "path": "/music/let_it_be.flac",
                "title": "Let It Be",
                "artist": "The Beatles",
                "album": "Let It Be",
                "duration": 243.0,
                "image_url": "",
            },
            "artist": "The Beatles",
            "playlist": None,
            "intent": "play_now",
        }
        messages = [
            {"sender": "user",      "text": "play let it be"},
            {"sender": "assistant", "text": "Playing Let It Be.", "entities": entities},
            {"sender": "user",      "text": "play similar"},
        ]
        runner, engine, _ = _make_runner(tracks=_SAMPLE_TRACKS, artists=[])
        intent = _make_intent(name="play_similar", query="")
        resolved_intent, engine_after, _ = _run_anaphora(runner, messages, intent)
        self.assertEqual(resolved_intent.extras.get("seed_path_override"), "/music/let_it_be.flac",
                         "Entity-dict end-to-end: seed_path_override not set from entities.track.path.")
        self.assertEqual(engine_after.current_path, "",
                         "engine state was mutated during _resolve_anaphora.")


def tearDownModule():
    # Remove the stubs we injected so they do not leak to other test suites
    for m in ["utils.dsp", "utils.track_graph", "flet", "flet_core"]:
        if m in _original_modules:
            sys.modules[m] = _original_modules[m]
        else:
            sys.modules.pop(m, None)



if __name__ == "__main__":
    unittest.main(verbosity=2)

