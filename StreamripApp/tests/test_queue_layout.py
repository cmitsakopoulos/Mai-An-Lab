"""Queue layout while auto-play is on: [playing][up next][station][held].
The listener's picks play before recommendations, and the held queue is
untouched until auto-play is off."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils import queue_layout  # noqa: E402
from utils.audio_engine import AudioEngine  # noqa: E402


def _row(path, **tags):
    return {"path": path, **tags}


def _paths(q):
    return [t["path"] for t in q]


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.q = [
            _row("/now"),
            _row("/u/1", _queued=True),
            _row("/u/2", _queued=True),
            _row("/r/1", _autoplay=True),
            _row("/r/2", _autoplay=True),
            _row("/lib/1"),
            _row("/lib/2"),
        ]

    def test_up_next_block_ends_at_the_first_unqueued_row(self):
        self.assertEqual(queue_layout.up_next_end(self.q, 0), 3)

    def test_held_queue_starts_after_the_last_recommendation(self):
        self.assertEqual(queue_layout.held_start(self.q, 0), 5)

    def test_without_recommendations_held_starts_after_up_next(self):
        q = [r for r in self.q if not r.get("_autoplay")]
        self.assertEqual(queue_layout.held_start(q, 0), 3)

    def test_once_past_the_station_everything_after_the_playing_row_is_held(self):
        # The station ran dry (e.g. no UI session to refill) and playback fell
        # into the held queue.
        self.assertEqual(queue_layout.held_start(self.q, 5), 6)

    def test_as_queued_copies_and_drops_the_recommendation_tag(self):
        src = _row("/x", _autoplay=True)
        out = queue_layout.as_queued(src)
        self.assertEqual(out, {"path": "/x", "_queued": True})
        self.assertEqual(src, {"path": "/x", "_autoplay": True})   # untouched


class EngineQueueAddTests(unittest.TestCase):
    """AudioEngine without a page: only the Python-side queue is exercised."""

    def setUp(self):
        self.e = AudioEngine()
        self.e.queue = [
            _row("/now"),
            _row("/r/1", _autoplay=True),
            _row("/r/2", _autoplay=True),
            _row("/lib/1"),
            _row("/lib/2"),
        ]
        self.e.current_index = 0

    def test_add_to_queue_plays_before_the_station_in_order(self):
        self.e.queue_last(_row("/a"))
        self.e.queue_last(_row("/b"))
        self.assertEqual(_paths(self.e.queue),
                         ["/now", "/a", "/b", "/r/1", "/r/2", "/lib/1", "/lib/2"])
        self.assertTrue(self.e.queue[1]["_queued"])

    def test_play_next_jumps_ahead_of_earlier_picks(self):
        self.e.queue_last(_row("/a"))
        self.e.queue_next(_row("/n"))
        self.assertEqual(_paths(self.e.queue)[:3], ["/now", "/n", "/a"])

    def test_add_to_queue_without_auto_play_goes_ahead_of_the_rest_of_the_queue(self):
        self.e.queue = [_row("/now"), _row("/album/2"), _row("/album/3")]
        self.e.queue_last(_row("/a"))
        self.assertEqual(_paths(self.e.queue), ["/now", "/a", "/album/2", "/album/3"])

    def test_add_to_queue_under_shuffle_still_appends(self):
        self.e._is_shuffle = True
        self.e.queue_last(_row("/a"))
        self.assertEqual(self.e.queue[-1]["path"], "/a")

    def test_adding_a_block_keeps_its_order_after_earlier_picks(self):
        self.e.queue_last(_row("/a"))
        self.e.queue_last_many([_row("/b/1"), _row("/b/2")])
        self.assertEqual(_paths(self.e.queue)[:5], ["/now", "/a", "/b/1", "/b/2", "/r/1"])
        self.assertTrue(all(t.get("_queued") for t in self.e.queue[1:4]))

    def test_the_callers_dict_is_not_tagged(self):
        meta = _row("/a")
        self.e.queue_last(meta)
        self.assertNotIn("_queued", meta)


if __name__ == "__main__":
    unittest.main()
