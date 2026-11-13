"""Auto-play: after the songs you queued yourself, similar songs from your
library keep playing.

While auto-play is on, the player holds exactly this after the playing song:

    [your picks]  Play Next / Add to Queue (utils.queue_layout.QUEUED)
    [auto-play]   a short buffer of recommendations (tagged `_autoplay`)

and nothing else, so nothing else can play next. The queue you were in when
auto-play took over (the rest of an album, a library list) is SET ASIDE in
`held`, out of the player, and put back after the playing song when auto-play
is turned off. That replaces the old "held queue" that stayed in the player
below the buffer: whenever the buffer was empty (a refill in flight, a fast
skip, a station played out, an unanalysed song) the player fell straight into
it and played it.

The station (`track_graph.Station`) is opened from one track, the anchor, and
stays inside that track's genre umbrella (`artist_graph.UMBRELLA`). Each refill
continues it; nothing it has queued comes back. When the umbrella is played out
the station starts the genre over (with a notice) rather than leaving it.

Starting a station: turning auto-play on, choosing a track (a tap, Start radio,
or any new queue set elsewhere) starts one from that track. Playing on through
your picks or the recommendations carries the station on.

If no station can start from a song (not analysed yet, or nothing similar), the
set-aside queue is put back with a notice, and the next song that plays from it
gets a fresh try.

Negative feedback: removing a recommendation, or skipping one early (see
play_ledger.is_listen), steers the CURRENT station away from that artist;
NEGATIVES_TO_REPLAN of them replace the unheard buffer. It is forgotten when
the next station starts: auto-play keeps no model of the listener's taste.

Cost: nothing runs on a timer. A track change may top the buffer up (one
resumable take + one batched DB read); a station opens once.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque

from utils import play_ledger, queue_layout, track_graph

logger = logging.getLogger(__name__)

BUFFER_TARGET = 4        # tracks kept queued ahead while in the foreground
BUFFER_LOW = 3           # top up when fewer than this remain
BACKGROUND_TARGET = 24   # ~1.5 h, topped up once before the UI goes away
HISTORY_LEN = 150        # tracks started this session, never re-recommended
SKIP_SIGNAL_MAX_AGE_S = 180  # older ledger entries are history, not feedback
# A fill is one take plus one batched DB read: seconds at most. One still marked
# running after this long never ran: it was scheduled on a UI session the OS
# tore down, so its `finally` never cleared the flag.
FILL_STALE_S = 60.0
# Starting the genre over: the last few songs heard are kept out of the restart
# so the end of the station isn't replayed straight away.
RESTART_GAP = 5

# Station shape. Measured on the device image (2026-09-28): 5 per act / 1 per
# album keeps a same-artist run to ~4 across different albums (never inside one
# release). The caps count over the last CAP_WINDOW tracks.
ARTIST_CAP = 5
ALBUM_CAP = 1
CAP_WINDOW = 24
NEGATIVES_TO_REPLAN = 2


class AutoPlay:
    """Owns auto-play state and every queue edit it makes. `run_task(fn, *args)`
    schedules a coroutine function on the UI loop; `notify(message)` surfaces a
    user-facing hint."""

    def __init__(self, engine, db, run_task, notify=None):
        self.engine = engine
        self.db = db
        self._run_task = run_task
        self._notify = notify or (lambda _msg: None)

        self._enabled = False
        self.anchor = ""                 # the track the station started from
        self.station: track_graph.Station | None = None
        self.held: list[dict] = []       # the queue auto-play set aside
        self.history: deque[str] = deque(maxlen=HISTORY_LEN)
        self._negatives: list[str] = []
        # Tracks the station has queued since it (re)opened: a station that
        # runs out after queuing nothing is empty, not played out.
        self._taken = 0
        self._started_over = False       # the notice is said once per station
        # A start just failed: don't repeat the same notice on every track
        # while the set-aside queue plays through unanalysed songs.
        self._quiet = False
        self._dead = ""                  # the last anchor no station could start from
        # Generation: bumped whenever the plan changes (toggle, new station, a
        # replan) so a fill that was awaiting the station bails instead of
        # inserting a stale block.
        self._gen = 0
        self._filling = False
        self._fill_started = 0.0

    def bind_ui(self, run_task, notify=None) -> None:
        """Re-point the UI hooks at a new Flet session. The controller is kept
        per process, but the session it schedules work on does not. A fill
        scheduled on the old session may never run, so it is superseded."""
        self._run_task = run_task
        self._notify = notify or (lambda _msg: None)
        self._gen += 1
        self._filling = False

    # ── mode ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, on: bool) -> None:
        # The engine asks auto-play for more at the end of the queue only while on.
        self._enabled = bool(on)
        self.engine.continues_at_end = self._enabled

    def set_enabled(self, on: bool, seed: str = "") -> None:
        if on == self.enabled:
            return
        self.enabled = on
        if on:
            self.start(seed or self.engine.current_path)
        else:
            self._reset("")
            self._quiet, self._dead = False, ""
            self.drop_buffer()
            self._restore_held()

    def start(self, path: str) -> None:
        """Start a new station from `path` (playing now): set the rest of the
        queue aside and replace the pending buffer."""
        if not self.enabled or not path:
            return
        self._reset(path)
        self._set_aside()
        self._schedule_fill(BUFFER_TARGET, replace=True)

    def resume(self, path: str) -> None:
        """A session (re)started on `path`: give an enabled station its anchor
        back and top the buffer up. (The station reopens on that fill; its
        state isn't persisted.)"""
        if not self.enabled or not path:
            return
        if self._foreign_rows():
            self.start(path)
            return
        if not self.anchor:
            self.anchor = path
        self.ensure_buffer()

    def _reset(self, anchor: str) -> None:
        self._gen += 1
        self._filling = False
        self.anchor = anchor or ""
        self.station = None
        self._negatives = []
        self._taken = 0
        self._started_over = False

    @property
    def current_genre(self) -> str | None:
        """The genre the station is playing now, for display."""
        return self.station.flavour if self.station is not None else None

    # ── events from the app ─────────────────────────────────────────────────
    def on_track_changed(self, path: str) -> None:
        """The playing track changed (skip, auto-advance, a tap, a new queue)."""
        if path:
            self.history.append(path)
            if self.station is not None:
                self.station.exclude([path])
        if not self.enabled or not path:
            return
        row = self._current_row()
        if row.get("path") != path or path == self._dead:
            # The engine has moved on again (a later change is on its way), or
            # no station can start from this song (already said so).
            self.ensure_buffer()
            return
        carries_on = (path == self.anchor or bool(row.get(queue_layout.AUTOPLAY))
                      or bool(row.get(queue_layout.QUEUED)))
        if not carries_on or self._foreign_rows():
            # Not a pick or a recommendation: a new queue was set (from any
            # view, Jarvis, a restore) or the listener went back into history.
            self.start(path)
            return
        self.ensure_buffer()

    def on_user_chose(self, path: str) -> None:
        """The user explicitly chose `path`; the engine is already on it. A
        pending recommendation just plays (the station carries on); anything
        else starts a new station from it."""
        if not self.enabled or not path:
            return
        row = self._current_row()
        if self.station is not None and row.get("_autoplay") and row.get("path") == path:
            self.ensure_buffer()
            return
        self.start(path)

    def on_listens(self, entries: list[dict]) -> None:
        """Ledger batch (play order): {path, listened_s, duration_s, ended_at}."""
        now = time.time()
        for e in entries:
            # Heard while the UI was away: still history.
            path = e.get("path") or ""
            if path and path not in self.history:
                self.history.append(path)
            ended = e.get("ended_at") or now
            self.on_listen(path, float(e.get("listened_s") or 0.0),
                           float(e.get("duration_s") or 0.0),
                           fresh=(now - ended) <= SKIP_SIGNAL_MAX_AGE_S)

    def on_listen(self, path: str, listened_s: float, duration_s: float,
                  fresh: bool = True) -> None:
        """One finished listen. A track listened through pulls the station
        toward its artist; an early skip of a recommendation is a negative."""
        if not path:
            return
        if play_ledger.is_listen(listened_s, duration_s):
            if self.station is not None:
                self.station.accept(path)
            return
        if self.enabled and fresh and self._is_autoplay(path):
            self._negative(path)

    def reject(self, path: str) -> None:
        """The listener removed a recommendation (call AFTER the row is gone):
        steer this station away from it and top the buffer back up."""
        if not path:
            return
        if self.station is not None:
            self.station.exclude([path])
        if self.enabled and not self._negative(path):
            have = len(self._pending_rows())
            if not self._fill_busy() and have < BUFFER_TARGET:
                self._schedule_fill(BUFFER_TARGET - have)

    def on_queue_dry(self) -> None:
        """The queue played out while enabled: append a block and play it."""
        if not self.enabled or self._fill_busy():
            return
        self._schedule_fill(BUFFER_TARGET, append_and_play=True)

    def prefill_for_background(self) -> None:
        """Top the buffer up before the UI session may be torn down: with the
        session gone nothing refills, and playback stops at the end."""
        self.ensure_buffer(BACKGROUND_TARGET)

    # ── history ─────────────────────────────────────────────────────────────
    async def load_history(self) -> None:
        """History is in-memory; on a cold start recent plays stand in for it,
        oldest first like live history."""
        if self.history:
            return
        try:
            rows = await self.db.get_recent_tracks(HISTORY_LEN)
        except Exception:
            logger.warning("Auto-play: could not load recent plays", exc_info=True)
            return
        for row in reversed(rows or []):
            path = row.get("path") if isinstance(row, dict) else None
            if path and path not in self.history:
                self.history.append(path)

    # ── queue ───────────────────────────────────────────────────────────────
    def _current_row(self) -> dict:
        q, ci = self.engine.queue, self.engine.current_index
        return q[ci] if 0 <= ci < len(q) else {}

    def _foreign_rows(self) -> list[int]:
        """Rows ahead that are neither the listener's picks nor auto-play's:
        while auto-play is on these get set aside."""
        q = self.engine.queue
        start = queue_layout.up_next_end(q, self.engine.current_index)
        return [i for i in range(start, len(q)) if not q[i].get(queue_layout.AUTOPLAY)]

    def _set_aside(self) -> None:
        rows = self._foreign_rows()
        if not rows:
            return
        q = self.engine.queue
        self.held = [q[i] for i in rows]
        self.engine.remove_indices(rows)

    def _restore_held(self) -> None:
        """Put the set-aside queue back after the listener's picks."""
        held, self.held = self.held, []
        if not held or not self.engine.queue:
            return
        at = queue_layout.up_next_end(self.engine.queue, self.engine.current_index)
        self.engine.queue_after_current(held, after_index=at - 1)

    def _pending_rows(self) -> list[int]:
        q = self.engine.queue
        return [i for i in range(self.engine.current_index + 1, len(q))
                if q[i].get(queue_layout.AUTOPLAY)]

    def buffer_paths(self) -> list[str]:
        """The pending auto-play tracks ahead of the playing row, in order."""
        q = self.engine.queue
        return [q[i]["path"] for i in self._pending_rows() if q[i].get("path")]

    def drop_buffer(self) -> int:
        """Remove every pending auto-play track (never the playing one)."""
        victims = self._pending_rows()
        if victims:
            self.engine.remove_indices(victims)
        return len(victims)

    def ensure_buffer(self, target: int = BUFFER_TARGET) -> None:
        if not self.enabled or self._fill_busy():
            return
        have = len(self._pending_rows())
        low = BUFFER_LOW if target == BUFFER_TARGET else target
        if have < low:
            self._schedule_fill(target - have)

    # ── internals ───────────────────────────────────────────────────────────
    def _negative(self, path: str) -> bool:
        """Count a rejection and steer the station away from it; True when it
        replanned (the buffer is being replaced)."""
        self._negatives.append(path)
        if self.station is not None:
            self.station.reject([path])
        if len(self._negatives) >= NEGATIVES_TO_REPLAN:
            self._negatives = []
            if self.station is not None:
                self._gen += 1            # supersede a fill already in flight
                self._filling = False
                self._schedule_fill(BUFFER_TARGET, replace=True)
            return True
        return False

    def _is_autoplay(self, path: str) -> bool:
        return any(t.get(queue_layout.AUTOPLAY) and t.get("path") == path
                   for t in self.engine.queue)

    def _avoid(self, recent: int | None = None) -> set[str]:
        """What a station must not pick: queued recommendations, the playing
        track and this session's history (only the last `recent` of it when
        the genre starts over)."""
        avoid = set(self.buffer_paths())
        hist = list(self.history)
        avoid.update(hist if recent is None else hist[-recent:])
        if self.engine.current_path:
            avoid.add(self.engine.current_path)
        return avoid

    def _insert_after(self) -> int:
        """Below the last pending recommendation, or below the listener's
        up-next picks, which always play first."""
        rows = self._pending_rows()
        if rows:
            return rows[-1]
        return queue_layout.up_next_end(self.engine.queue, self.engine.current_index) - 1

    def _fill_busy(self) -> bool:
        if self._filling and time.monotonic() - self._fill_started > FILL_STALE_S:
            logger.error("Auto-play: a fill never finished; clearing it")
            self._filling = False
        return self._filling

    def _schedule_fill(self, n: int, *, replace: bool = False,
                       append_and_play: bool = False) -> None:
        if n <= 0 or (self._fill_busy() and not replace):
            return
        self._filling = True
        self._fill_started = time.monotonic()
        try:
            self._run_task(self._fill, n, self._gen, replace, append_and_play)
        except Exception:
            # No live session to run it on (torn down mid-call).
            self._filling = False
            logger.error("Auto-play: could not schedule a fill", exc_info=True)

    async def _open(self, recent: int | None = None) -> track_graph.Station | None:
        seed = self.anchor or self.engine.current_path
        if not seed:
            return None
        self.anchor = seed
        return await track_graph.open_station(
            self.db, seed, avoid=self._avoid(recent), focused=True,
            max_per_artist=ARTIST_CAP, max_per_album=ALBUM_CAP, window=CAP_WINDOW,
        )

    async def _take(self, n: int, gen: int) -> tuple[list[str], bool]:
        """Up to `n` paths from the station, opening it on first use and
        starting the genre over when it is played out. (paths, just_opened)."""
        opened = self.station is None
        if opened:
            station = await self._open()
            if station is None or gen != self._gen:
                return [], opened
            self.station = station
            logger.info("Auto-play: station from %s", os.path.basename(self.anchor))
        paths = await self.station.take(n)
        self._taken += len(paths)
        if len(paths) < n and self._taken > 0:
            # Played out: start the genre over from the anchor.
            genre = self.station.flavour or "similar songs"
            station = await self._open(recent=RESTART_GAP)
            if station is None or gen != self._gen:
                return paths, opened
            station.exclude(paths)
            more = [p for p in await station.take(n - len(paths)) if p not in paths]
            self.station = station
            self._taken = len(more)   # 0: nothing left even starting over
            if more and not self._started_over:
                self._started_over = True
                self._notify(f"Played all the {genre} in your library. Starting over.")
            paths += more
        return paths, opened

    async def _fill(self, n: int, gen: int, replace: bool = False,
                    append_and_play: bool = False) -> None:
        try:
            paths, opened = await self._take(n, gen)
            if gen != self._gen or not self.enabled:
                return
            batch = await self._tracks(paths)
            if gen != self._gen or not self.enabled:
                return
            if not batch and opened:
                await self._cannot_start()
                return
            if batch:
                self._quiet, self._dead = False, ""
            if append_and_play:
                if not batch:
                    self.engine.stop()
                    return
                first_new = len(self.engine.queue)
                self.engine.queue_extend(batch)
                self.engine.play_track_at(first_new)
            elif replace:
                victims = self._pending_rows()
                if victims:
                    self.engine.replace_rows(victims, batch)
                elif batch:
                    self.engine.queue_after_current(batch, after_index=self._insert_after())
            elif batch:
                self.engine.queue_after_current(batch, after_index=self._insert_after())
            if batch:
                logger.info("Auto-play: queued %d from %s", len(batch),
                            os.path.basename(self.anchor))
        except Exception:
            logger.exception("Auto-play: fill failed")
        finally:
            if gen == self._gen:
                self._filling = False

    async def _cannot_start(self) -> None:
        """No station from the anchor: drop the old station's buffer and give
        back the set-aside queue. The next song that plays from it is a fresh
        start (see on_track_changed)."""
        self._dead = self.anchor
        self.drop_buffer()
        self._restore_held()
        if self._quiet:
            return
        self._quiet = True
        try:
            analysed = await self.db.has_current_features(self.anchor, track_graph.FEATURES_VERSION)
        except Exception:
            analysed = True
        if not analysed:
            self._notify("This song isn't analysed yet, so auto-play can't start "
                         "from it. Your queue continues.")
        else:
            self._notify("No similar songs found for this one. Your queue continues.")

    async def _tracks(self, paths: list[str]) -> list[dict]:
        """Engine track dicts for `paths`, tagged as recommendations."""
        if not paths:
            return []
        rows = await self.db.get_tracks_brief(paths)
        batch = []
        for p in paths:
            row = rows.get(p)
            if row is None:
                continue
            batch.append({
                "path":        p,
                "track_title": row.get("title") or os.path.basename(p),
                "artist_name": row.get("artist") or "Unknown Artist",
                "album_title": row.get("album") or "Unknown Album",
                "duration":    row.get("duration") or 0.0,
                queue_layout.AUTOPLAY: True,
            })
        return batch
