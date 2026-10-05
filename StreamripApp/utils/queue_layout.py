"""How the queue after the playing row is laid out, shared by both engines,
auto-play and the queue sheet.

    [playing] [up next] [auto-play station] [held]

Up next     the listener's own picks (Play Next / Add to Queue), tagged
            QUEUED. They always play before auto-play's recommendations.
Station     auto-play's pending recommendations (tagged `_autoplay`), topped up
            just below the last one.
Held        everything after: the queue auto-play paused. Shown dimmed and
            inert while auto-play is on; turning auto-play off drops the unheard
            station rows, so the held queue is next again exactly where it
            stopped. (If the station runs dry, e.g. with the UI session gone,
            playback falls through into it rather than stopping.)

Nothing here is a second list: it is one queue, one native playlist, read
three ways. With auto-play off there is no station, so "held" is just the rest
of the queue.
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


def held_start(queue: list[dict], current_index: int) -> int:
    """First held row: past the last pending recommendation, or past the
    up-next block while none is pending (a refill in flight, or a dry station).
    Everything before it plays while auto-play stays on."""
    last = None
    for i in range(max(current_index + 1, 0), len(queue)):
        if queue[i].get(AUTOPLAY):
            last = i
    return last + 1 if last is not None else up_next_end(queue, current_index)
