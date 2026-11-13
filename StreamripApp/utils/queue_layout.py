"""How the queue after the playing row is laid out, shared by both engines,
auto-play and the queue sheet.

    [playing] [up next] [auto-play]

Up next     the listener's own picks (Play Next / Add to Queue), tagged
            QUEUED. They always play before auto-play's recommendations.
Auto-play   pending recommendations (tagged `_autoplay`), topped up just below
            the last one. While auto-play is on nothing else is in the player
            after the playing row: the rest of the queue is set aside outside
            it (utils.autoplay) and comes back when auto-play is turned off.

With auto-play off, everything after the up-next block is just the rest of the
queue.
"""
from __future__ import annotations

QUEUED = "_queued"
AUTOPLAY = "_autoplay"


def as_queued(track: dict) -> dict:
    """A copy of `track` marked as the listener's pick (callers often pass
    dicts they keep using, so the tag never leaks back into them)."""
    row = dict(track)
    row.pop(AUTOPLAY, None)
    row[QUEUED] = True
    return row


def up_next_end(queue: list[dict], current_index: int) -> int:
    """Index just past the up-next block: the contiguous QUEUED rows right
    after the playing one. Add to Queue inserts here (first in, first out)."""
    i = max(current_index + 1, 0)
    while i < len(queue) and queue[i].get(QUEUED):
        i += 1
    return min(i, len(queue))

